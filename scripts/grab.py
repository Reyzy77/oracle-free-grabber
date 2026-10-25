#!/usr/bin/env python3
"""
Oracle Cloud Free Tier instance grabber.

Boucle sans jamais s'arreter tant qu'aucune machine n'est obtenue.
Essaie TOUJOURS la plus grosse machine Always Free possible :
VM.Standard.A1.Flex -- 4 OCPU / 24 GB RAM (ARM Ampere).

Tout est configure par variables d'environnement (voir README.md).
"""

import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone

import oci
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SHAPE = os.environ.get("OCI_SHAPE", "VM.Standard.A1.Flex")

# Echelle de tailles, de la plus grosse a la plus petite.
# Par defaut : UNIQUEMENT 4 OCPU / 24 GB (le maximum Always Free).
SHAPE_LADDER = os.environ.get("SHAPE_LADDER", "4:24").strip()

BOOT_VOLUME_GB = int(os.environ.get("BOOT_VOLUME_GB", "50"))
OS_NAME = os.environ.get("OS_NAME", "Canonical Ubuntu")
OS_VERSION = os.environ.get("OS_VERSION", "24.04")
DISPLAY_NAME = os.environ.get("DISPLAY_NAME", "free-a1-grabbed")

# Secondes entre deux tentatives (randomise pour eviter le rate-limit)
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "60"))

# 0 = infini (usage local). En CI on met ~330 min pour rester sous la limite
# de 6h d'un job GitHub Actions ; le cron relance immediatement apres.
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "0"))

SSH_PUBLIC_KEY = os.environ.get("SSH_PUBLIC_KEY", "").strip()

# Messages qui signifient "pas de capacite, reessaie plus tard"
CAPACITY_MARKERS = (
    "out of host capacity",
    "out of capacity",
    "outofcapacity",
    "insufficient capacity",
)

STOP_MARKER = os.environ.get("STOP_MARKER", "SUCCESS.txt")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print("[" + ts + "Z] " + str(msg), flush=True)


def die(msg):
    log("FATAL: " + str(msg))
    sys.exit(2)


def parse_ladder():
    out = []
    for item in SHAPE_LADDER.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            o, m = item.split(":")
            out.append((float(o), float(m)))
        except ValueError:
            die("SHAPE_LADDER invalide: " + item + " (format attendu 4:24,2:12)")
    if not out:
        die("SHAPE_LADDER est vide")
    # Du plus gros au plus petit
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return out


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def build_config():
    """Construit la config OCI depuis les variables d'environnement."""
    required = ["OCI_USER_OCID", "OCI_TENANCY_OCID", "OCI_FINGERPRINT", "OCI_REGION"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        die("Variables manquantes: " + ", ".join(missing))

    key_content = os.environ.get("OCI_KEY_CONTENT", "")
    key_file = os.environ.get("OCI_KEY_FILE", "")

    if key_content:
        # Les secrets peuvent arriver avec des \n litteraux (copier-coller)
        # ou des fins de ligne Windows (pipe PowerShell) : on normalise.
        key_content = key_content.replace("\\n", "\n")
        key_content = key_content.replace("\r\n", "\n").replace("\r", "\n")
        key_content = key_content.strip() + "\n"
        if "PRIVATE KEY" not in key_content:
            die("OCI_KEY_CONTENT ne ressemble pas a une cle PEM "
                "(aucune ligne BEGIN ... PRIVATE KEY trouvee)")
        key_file = os.path.abspath("oci_api_key.pem")
        with open(key_file, "w", newline="\n") as fh:
            fh.write(key_content)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    elif not key_file:
        die("OCI_KEY_CONTENT ou OCI_KEY_FILE est requis")

    config = {
        "user": os.environ["OCI_USER_OCID"].strip(),
        "tenancy": os.environ["OCI_TENANCY_OCID"].strip(),
        "fingerprint": os.environ["OCI_FINGERPRINT"].strip(),
        "region": os.environ["OCI_REGION"].strip(),
        "key_file": key_file,
    }
    passphrase = os.environ.get("OCI_KEY_PASSPHRASE")
    if passphrase:
        config["pass_phrase"] = passphrase

    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as err:
        hints = {
            "user": "doit commencer par ocid1.user.",
            "tenancy": "doit commencer par ocid1.tenancy.",
            "fingerprint": "format attendu aa:bb:cc:... (32 octets hex)",
            "region": "identifiant de region, ex. eu-paris-1",
            "key_file": "chemin de la cle privee introuvable",
        }
        details = ", ".join(
            k.upper() + " " + str(v) + " (" + hints.get(k, "") + ")"
            for k, v in err.args[0].items()
        )
        die("Secrets OCI invalides -> " + details)
    return config


# --------------------------------------------------------------------------
# Decouverte des ressources (image, AD, subnet)
# --------------------------------------------------------------------------

def find_image(compute, compartment_id):
    """Derniere image ARM (aarch64) compatible avec la shape demandee."""
    images = oci.pagination.list_call_get_all_results(
        compute.list_images,
        compartment_id=compartment_id,
        operating_system=OS_NAME,
        operating_system_version=OS_VERSION,
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        die("Aucune image " + OS_NAME + " " + OS_VERSION + " pour la shape " + SHAPE)

    arm = [i for i in images if "aarch64" in (i.display_name or "").lower()]
    chosen = (arm or images)[0]
    log("Image: " + chosen.display_name + " (" + chosen.id + ")")
    return chosen.id


def find_availability_domains(identity, compartment_id):
    ads = identity.list_availability_domains(compartment_id=compartment_id).data
    names = [ad.name for ad in ads]
    log("Availability domains: " + str(names))
    return names


def find_or_create_subnet(vnet, compartment_id):
    """Reutilise un subnet public existant, sinon cree VCN + IGW + subnet."""
    override = os.environ.get("OCI_SUBNET_OCID", "").strip()
    if override:
        log("Subnet (fourni): " + override)
        return override

    subnets = oci.pagination.list_call_get_all_results(
        vnet.list_subnets, compartment_id=compartment_id
    ).data
    usable = [s for s in subnets
              if s.lifecycle_state == "AVAILABLE" and not s.prohibit_public_ip_on_vnic]
    if usable:
        log("Subnet existant reutilise: " + usable[0].display_name
            + " (" + usable[0].id + ")")
        return usable[0].id

    log("Aucun subnet public trouve -> creation du reseau (VCN + IGW + subnet)")

    vcn = vnet.create_vcn(oci.core.models.CreateVcnDetails(
        compartment_id=compartment_id,
        cidr_block="10.0.0.0/16",
        display_name="grab-vcn",
        dns_label="grabvcn",
    )).data
    oci.wait_until(vnet, vnet.get_vcn(vcn.id), "lifecycle_state", "AVAILABLE",
                   max_wait_seconds=300)

    igw = vnet.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=compartment_id,
        vcn_id=vcn.id,
        is_enabled=True,
        display_name="grab-igw",
    )).data
    oci.wait_until(vnet, vnet.get_internet_gateway(igw.id), "lifecycle_state",
                   "AVAILABLE", max_wait_seconds=300)

    vnet.update_route_table(
        vcn.default_route_table_id,
        oci.core.models.UpdateRouteTableDetails(route_rules=[
            oci.core.models.RouteRule(
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                network_entity_id=igw.id,
            )
        ]),
    )

    # Ouvre SSH en entree
    sl_id = vcn.default_security_list_id
    sl = vnet.get_security_list(sl_id).data
    rules = list(sl.ingress_security_rules)
    rules.append(oci.core.models.IngressSecurityRule(
        protocol="6", source="0.0.0.0/0",
        tcp_options=oci.core.models.TcpOptions(
            destination_port_range=oci.core.models.PortRange(min=22, max=22)
        ),
    ))
    vnet.update_security_list(
        sl_id, oci.core.models.UpdateSecurityListDetails(ingress_security_rules=rules)
    )

    subnet = vnet.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=compartment_id,
        vcn_id=vcn.id,
        cidr_block="10.0.0.0/24",
        display_name="grab-subnet",
        dns_label="grabsubnet",
        prohibit_public_ip_on_vnic=False,
    )).data
    oci.wait_until(vnet, vnet.get_subnet(subnet.id), "lifecycle_state", "AVAILABLE",
                   max_wait_seconds=300)

    log("Subnet cree: " + subnet.id)
    return subnet.id


def already_have_instance(compute, compartment_id):
    """Evite les doublons : une machine de la bonne shape tourne-t-elle deja ?"""
    insts = oci.pagination.list_call_get_all_results(
        compute.list_instances, compartment_id=compartment_id
    ).data
    return [i for i in insts
            if i.shape == SHAPE
            and i.lifecycle_state in ("RUNNING", "PROVISIONING", "STARTING", "STOPPED")]


# --------------------------------------------------------------------------
# Lancement
# --------------------------------------------------------------------------

def report_success(compute, vnet, instance, compartment_id):
    log("=" * 70)
    log("MACHINE OBTENUE !")
    log("  Nom     : " + str(instance.display_name))
    log("  OCID    : " + str(instance.id))
    log("  Shape   : " + str(instance.shape))
    if instance.shape_config:
        log("  Taille  : " + str(instance.shape_config.ocpus) + " OCPU / "
            + str(instance.shape_config.memory_in_gbs) + " GB RAM")
    log("  AD      : " + str(instance.availability_domain))

    ip = "(en cours d'attribution)"
    try:
        vas = compute.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance.id).data
        if vas:
            vnic = vnet.get_vnic(vas[0].vnic_id).data
            ip = vnic.public_ip or vnic.private_ip
    except Exception:
        pass
    log("  IP      : " + str(ip))
    log("=" * 70)

    with open(STOP_MARKER, "w") as fh:
        fh.write("instance_id=" + str(instance.id) + "\n")
        fh.write("display_name=" + str(instance.display_name) + "\n")
        fh.write("shape=" + str(instance.shape) + "\n")
        if instance.shape_config:
            fh.write("ocpus=" + str(instance.shape_config.ocpus) + "\n")
            fh.write("memory_gb=" + str(instance.shape_config.memory_in_gbs) + "\n")
        fh.write("availability_domain=" + str(instance.availability_domain) + "\n")
        fh.write("public_ip=" + str(ip) + "\n")

    # Sortie exploitable par GitHub Actions
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as fh:
            fh.write("grabbed=true\n")
            fh.write("instance_ip=" + str(ip) + "\n")
            fh.write("instance_id=" + str(instance.id) + "\n")


def main():
    if not SSH_PUBLIC_KEY:
        die("SSH_PUBLIC_KEY est requis (ta cle publique SSH, une seule ligne)")

    ladder = parse_ladder()
    config = build_config()

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    vnet = oci.core.VirtualNetworkClient(config)

    # Pas de retry interne du SDK : on gere la boucle nous-memes.
    no_retry = oci.retry.NoneRetryStrategy()

    compartment_id = os.environ.get("OCI_COMPARTMENT_OCID", "").strip() \
        or config["tenancy"]

    log("Region      : " + config["region"])
    log("Compartment : " + compartment_id)
    log("Shape       : " + SHAPE)
    log("Tailles     : " + ", ".join(
        str(int(o)) + " OCPU / " + str(int(m)) + " GB" for o, m in ladder))
    log("Boot volume : " + str(BOOT_VOLUME_GB) + " GB")

    existing = already_have_instance(compute, compartment_id)
    if existing and os.environ.get("ALLOW_DUPLICATE", "false").lower() != "true":
        log("Une instance " + SHAPE + " existe deja : " + existing[0].display_name
            + " (" + existing[0].lifecycle_state + "). Rien a faire.")
        report_success(compute, vnet, compute.get_instance(existing[0].id).data,
                       compartment_id)
        return 0

    image_id = find_image(compute, compartment_id)
    ads = find_availability_domains(identity, compartment_id)
    subnet_id = find_or_create_subnet(vnet, compartment_id)

    started = time.time()
    attempt = 0
    backoff = 0

    while True:
        if MAX_RUNTIME_MIN and (time.time() - started) > MAX_RUNTIME_MIN * 60:
            log("Limite de duree atteinte (" + str(MAX_RUNTIME_MIN)
                + " min) sans succes. Le cron relancera. "
                + str(attempt) + " tentatives effectuees.")
            return 75  # code dedie : "pas de capacite, a relancer"

        for ocpus, memory in ladder:
            for ad in ads:
                attempt += 1
                label = ("#" + str(attempt) + " " + ad.split(":")[-1] + " "
                         + str(int(ocpus)) + "c/" + str(int(memory)) + "g")

                details = LaunchInstanceDetails(
                    compartment_id=compartment_id,
                    availability_domain=ad,
                    shape=SHAPE,
                    display_name=DISPLAY_NAME,
                    shape_config=LaunchInstanceShapeConfigDetails(
                        ocpus=ocpus, memory_in_gbs=memory),
                    source_details=InstanceSourceViaImageDetails(
                        image_id=image_id,
                        boot_volume_size_in_gbs=BOOT_VOLUME_GB),
                    create_vnic_details=CreateVnicDetails(
                        subnet_id=subnet_id, assign_public_ip=True),
                    metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
                )

                try:
                    resp = compute.launch_instance(details, retry_strategy=no_retry)
                    inst = resp.data
                    log(label + " -> ACCEPTE, provisioning de " + inst.id)
                    inst = oci.wait_until(
                        compute, compute.get_instance(inst.id),
                        "lifecycle_state", "RUNNING", max_wait_seconds=900,
                    ).data
                    report_success(compute, vnet, inst, compartment_id)
                    return 0

                except oci.exceptions.ServiceError as err:
                    msg = (err.message or "").lower()
                    code = err.code or ""
                    status = err.status

                    if any(m in msg for m in CAPACITY_MARKERS) or status == 500:
                        log(label + " -> pas de capacite ("
                            + str(status) + " " + str(code) + ")")
                        backoff = 0
                    elif status == 429 or code == "TooManyRequests":
                        backoff = min(backoff * 2 if backoff else 30, 600)
                        log(label + " -> rate-limit, pause " + str(backoff) + "s")
                        time.sleep(backoff)
                    elif code in ("LimitExceeded", "QuotaExceeded"):
                        # Quota atteint : soit la machine existe deja, soit la
                        # taille demandee depasse le free tier.
                        log(label + " -> quota/limite: " + str(err.message))
                        live = already_have_instance(compute, compartment_id)
                        if live:
                            log("Instance deja presente, on s'arrete.")
                            report_success(
                                compute, vnet,
                                compute.get_instance(live[0].id).data, compartment_id)
                            return 0
                        time.sleep(300)
                    elif status in (401, 403):
                        die("Auth refusee (" + str(status) + " " + str(code)
                            + "): " + str(err.message))
                    elif status == 404 and "subnet" in msg:
                        die("Subnet introuvable: " + str(err.message))
                    else:
                        log(label + " -> erreur " + str(status) + " " + str(code)
                            + ": " + str(err.message))

                except Exception as err:  # reseau, DNS, timeout...
                    log(label + " -> exception " + type(err).__name__ + ": " + str(err))

                time.sleep(RETRY_DELAY * random.uniform(0.6, 1.4))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrompu par l'utilisateur.")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
