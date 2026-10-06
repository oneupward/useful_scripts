#!/usr/bin/env python3
"""Lium GPU node installer, reviewed 2026-10-05. Python 3.10+, standard library only.

USAGE (on the GPU Ubuntu VM, not on your Mac):
  sudo python3 lium_ubuntu_installer.py install --provider vast
  sudo python3 lium_ubuntu_installer.py install --provider direct
  sudo python3 lium_ubuntu_installer.py check
  sudo python3 lium_ubuntu_installer.py diagnose

On another computer, after installation:
  python3 lium_ubuntu_installer.py probe --public-ip YOUR_IP \
    --api-public-port ACTUAL_API_PORT --ssh-public-port ACTUAL_SSH_PORT

The install command prompts for your PUBLIC registered miner hotkey and missing
network settings. It never asks for a mnemonic, coldkey, private key or portal JWT.
Use --help or 'install --help' for unattended configuration and repair options.

Scope: Ubuntu 22.04/24.04/26.04 amd64 with systemd and a real GPU VM/bare metal.
Docker, Compose, NVIDIA Container Toolkit, Sysbox, kernel modules, official Lium
runner/updater, local probes, logs, and a manual portal registration report.
Host NVIDIA driver installation is opt-in; it never reboots automatically.
No filesystem formatting, Docker data migration, wallet operations or chain fees.
No automatic cloud firewall/port allocation, node deletion or portal registration.

Installation can restart Docker on an EMPTY host. A runtime repair refuses ANY
existing container because the upstream Sysbox installer can delete containers.
Existing working Lium installations are detected; configuration changes require
--reconfigure. Active/stopped rental pods or unrelated containers block install.
Pause new rentals in the provider portal before maintenance; container checks
cannot prevent a new rental from starting between checks.
The official Lium runner may prune dangling images when it starts. Its deployment
uses privileged containers and the host Docker socket, as required by Lium.

Local success is NOT public reachability or validator acceptance. Run 'probe'
from another network, then add the endpoint through provider.lium.io. Rent ports
also need provider forwarding and the validator's actual workload checks.

Vast template example: retain existing options and add all of these TCP requests:
  -p 8080:8080 -p 2200:2200 -p 40000:40000 -p 40001:40001 -p 40002:40002
  -p 40003:40003 -p 40004:40004 -p 40005:40005 -p 40006:40006
  -p 40007:40007 -p 40008:40008 -p 40009:40009
These are TEMPLATE settings, not a shell command. Use the resulting public port
numbers in IP & Port Info. Editing a template does not itself update a live VM.

Exit codes: 0 completed (read the status), 1 failed/blocked, 2 invalid arguments,
194 driver installed and reboot required, 130 interrupted. Logs are root-only.

Sources (implementation follows these; this is an independent helper):
  https://github.com/Datura-ai/lium-io/tree/main/neurons/executor
  https://docs.lium.io/providers/portal/monitoring
  https://docs.lium.io/providers/portal/managing-nodes
  https://docs.docker.com/engine/install/ubuntu/
  https://documentation.ubuntu.com/server/how-to/graphics/install-nvidia-drivers/
  https://docs.vast.ai/linux-virtual-machines
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

VERSION = "1.0.1"
COMMIT = "2535a5af297a53ab038af4a46c52b162367f8a44"
UPSTREAM = "https://raw.githubusercontent.com/Datura-ai/lium-io/" + COMMIT + "/neurons/executor/"
HASHES = {
    "nvidia_docker_sysbox_setup.sh": "972a6a29cfb517d22a4a69aee2830c35d0a27bab1ba3179d6dd7e90eb26ffe32",
    "docker-compose.yml": "ae328698346e1d46ab876c3bb15893df7df23fcb96c0a2051cdbc515691ff367",
    ".env.template": "5a0a1b229235c85445d26f394a57066cb65d39682917632e4121f3a925c358fb",
}
STATE_DIR = Path("/var/lib/lium-ubuntu-installer")
LOG_DIR = Path("/var/log/lium-ubuntu-installer")
DEFAULT_DIR = Path("/opt/lium/neurons/executor")
RUNNER = "executor-executor-runner-1"
EXECUTOR = "executor-executor-1"
KNOWN = {RUNNER, EXECUTOR, "executor-watchtower-1", "executor-monitor-1", "executor-autoheal-1"}
PROBE_IMAGE = "daturaai/compute-subnet-executor:latest"
APT = ["apt-get", "-o", "DPkg::Lock::Timeout=180"]
LOG = None
LOG_PATH = None
STAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())


class Blocked(Exception):
    pass


def say(message):
    print(message, flush=True)
    if LOG:
        LOG.write(message + "\n")
        LOG.flush()


def run(argv, *, timeout=120, check=True, cwd=None, capture=False):
    """No shell/eval; output streams to screen and the private run log."""
    args = [str(x) for x in argv]
    if capture:
        result = subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=timeout, umask=0o022)
        if check and result.returncode:
            raise Blocked(f"Command failed ({result.returncode}): {shlex.join(args)}\n"
                          + (result.stderr or result.stdout)[-4000:])
        return result
    say("+ " + shlex.join(args))
    # A file keeps complete apt/pull output without pipe deadlocks or unbounded RAM.
    with tempfile.NamedTemporaryFile(mode="w+t") as output, open(output.name, "a") as sink:
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=sink, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True, umask=0o022)
        position, start, beat = 0, time.monotonic(), time.monotonic()
        try:
            while True:
                output.seek(position)
                chunk = output.read()
                position = output.tell()
                if chunk:
                    print(chunk, end="", flush=True)
                    if LOG:
                        LOG.write(chunk)
                        LOG.flush()
                if proc.poll() is not None:
                    output.seek(position)
                    tail = output.read()
                    if tail:
                        print(tail, end="", flush=True)
                        if LOG:
                            LOG.write(tail)
                            LOG.flush()
                    break
                if time.monotonic() - start > timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                if time.monotonic() - beat > 30:
                    say(f"Still running: {args[0]} ({int(time.monotonic()-start)} s)")
                    beat = time.monotonic()
                time.sleep(0.2)
        except BaseException:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
        if check and proc.returncode:
            raise Blocked(f"Command failed ({proc.returncode}): {shlex.join(args)}. See {LOG_PATH}.")
        return proc.returncode


def quiet(argv, timeout=20):
    try:
        result = run(argv, timeout=timeout, capture=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def docker(*args, **kwargs):
    return run(["docker", "--host", "unix:///var/run/docker.sock", *args], **kwargs)


def dq(*args):
    return quiet(["docker", "--host", "unix:///var/run/docker.sock", *args])


def dotenv(path):
    """Parse single-line data, never execute a provider or user .env file."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if not match:
            continue
        try:
            parts = shlex.split(match[2], comments=True)
            values[match[1]] = " ".join(parts)
        except ValueError:
            continue
    return values


def version_tuple(value):
    numbers = re.findall(r"\d+", value)
    return tuple(int(x) for x in (numbers + ["0", "0", "0"])[:3])


def port(value):
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,5}", text) or not 1 <= int(text) <= 65535:
        raise Blocked(f"Invalid TCP port: {text!r}; use the actual assigned port, 1-65535.")
    return int(text)


def public_ip(value):
    try:
        result = ipaddress.ip_address(value)
    except ValueError as e:
        raise Blocked("Enter one public IPv4 address, without a port or URL.") from e
    if result.version != 4 or not result.is_global:
        raise Blocked("The advertised IP must be a publicly routed IPv4 address.")
    return str(result)


def hotkey(value):
    if not 46 <= len(value) <= 50:
        raise Blocked("Enter a Bittensor SS58 PUBLIC address, not a mnemonic or token.")
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    try:
        number = 0
        for char in value:
            number = number * 58 + alphabet.index(char)
        raw = b"\0" * (len(value) - len(value.lstrip("1")))
        raw += number.to_bytes((number.bit_length() + 7) // 8, "big")
        digest = hashlib.blake2b(b"SS58PRE" + raw[:-2]).digest()[:2]
        valid = len(raw) == 35 and raw[0] == 42 and raw[-2:] == digest
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise Blocked("Invalid Bittensor SS58 public hotkey. Enter the registered PUBLIC address only.")
    return value


def expand_ports(value):
    result = []
    for item in value.split(","):
        pair = item.strip().split("-")
        if len(pair) == 1:
            result.append(port(pair[0]))
        elif len(pair) == 2:
            lo, hi = map(port, pair)
            if hi < lo or hi-lo > 1000:
                raise Blocked("Rental range is reversed or exceeds this installer's 1001-port limit.")
            result.extend(range(lo, hi+1))
        else:
            raise Blocked("Use a rental range such as 40000-40009, or a comma-separated list.")
    if len(set(result)) != len(result) or len(result) < 3 or len(result) > 1001:
        raise Blocked("Provide 3-1001 distinct rental ports.")
    return result


def mappings(value):
    try:
        items = json.loads(value)
        if not isinstance(items, list) or not 3 <= len(items) <= 1001:
            raise ValueError()
        result = []
        for pair in items:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(p) is not int for p in pair):
                raise ValueError()
            result.append([port(pair[0]), port(pair[1])])
        if len({p[0] for p in result}) != len(result) or len({p[1] for p in result}) != len(result):
            raise ValueError()
        return result
    except (ValueError, TypeError) as e:
        raise Blocked('Use at least three unique [VM_port, PUBLIC_port] pairs, e.g. '
                      "'[[40000,31001],[40001,31002],[40002,31003]]'.") from e


def prompt(label, value, *, unattended=False, default=None):
    if value not in (None, ""):
        return str(value)
    if unattended or not sys.stdin.isatty():
        if default is not None:
            return str(default)
        raise Blocked(f"Missing {label}; supply the corresponding command-line option.")
    answer = input(f"{label}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    if not answer and default is not None:
        return str(default)
    if not answer:
        raise Blocked(f"{label} is required.")
    return answer


def root_directory(path):
    path = Path(path)
    if not path.is_absolute() or any(c.isspace() for c in str(path)):
        raise Blocked("Installation directory must be an absolute path without whitespace.")
    # Never let a root installer write through paths controlled by another user.
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise Blocked(f"Symlink installation/configuration path refused: {candidate}")
        if candidate.exists():
            st = candidate.stat()
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise Blocked(f"Path must be root-owned and not group/world-writable: {candidate}")
    return path


def saved_record(filename):
    path = STATE_DIR / filename
    if not path.exists():
        return {}
    record = json.loads(path.read_text())
    if not isinstance(record, dict) or not isinstance(record.get("directory"), str):
        raise Blocked(f"Invalid saved configuration: {path}. Review it before continuing.")
    return record


def find_directory(explicit=None):
    mounted = dq("inspect", RUNNER, "--format", "{{json .Mounts}}")
    existing = None
    if mounted:
        for item in json.loads(mounted):
            if item.get("Destination") == "/root/executor/.env":
                existing = Path(item["Source"]).parent
    saved = saved_record("state.json").get("directory")
    pending = saved_record("deployment-pending.json").get("directory")
    candidates = [root_directory(p) for p in (explicit, existing, pending, saved) if p]
    if len({p.resolve() for p in candidates}) > 1:
        raise Blocked("Installation directory conflicts with the existing runner or saved deployment: "
                      + ", ".join(dict.fromkeys(map(str, candidates)))
                      + ". Use the original directory; this installer does not migrate deployments.")
    return candidates[0] if candidates else root_directory(DEFAULT_DIR)


def resolve_configuration(args, directory):
    old = dotenv(directory / ".env")
    provider_env = dotenv(Path("/etc/environment"))
    provider_env.update({k: v for k, v in os.environ.items()
                         if k.startswith("VAST_") or k == "PUBLIC_IPADDR"})
    previous = saved_record("state.json")
    if previous and Path(previous["directory"]).resolve() != directory.resolve():
        previous = {}
    provider = args.provider or previous.get("provider")
    if not provider and any(k.startswith("VAST_") for k in provider_env):
        provider = "vast"
    provider = prompt("Provider type (direct, vast, or nat)", provider,
                      unattended=args.non_interactive)
    if provider not in {"direct", "vast", "nat"}:
        raise Blocked("Provider must be direct, vast, or nat.")
    ask = lambda label, value, default=None: prompt(label, value, default=default,
                                                   unattended=args.non_interactive)
    cfg = {"provider": provider, "directory": str(directory), "script_version": VERSION,
           "reviewed_commit": COMMIT}
    cfg["hotkey"] = hotkey(ask("Registered miner PUBLIC hotkey",
                              args.hotkey or old.get("MINER_HOTKEY_SS58_ADDRESS")))
    cfg["api_port"] = port(args.api_port or old.get("EXTERNAL_PORT") or 8080)
    cfg["ssh_port"] = port(args.ssh_port or old.get("SSH_PORT") or 2200)
    cfg["internal_port"] = port(old.get("INTERNAL_PORT") or cfg["api_port"])
    cfg["public_ip"] = public_ip(ask("PUBLIC IPv4 from your provider panel",
                                    args.public_ip or provider_env.get("PUBLIC_IPADDR")
                                    or previous.get("public_ip")))
    same_network = (previous.get("provider") == provider
                    and previous.get("public_ip") == cfg["public_ip"])
    for key, option in (("api", args.api_public_port), ("ssh", args.ssh_public_port)):
        local = cfg[key + "_port"]
        detected = provider_env.get(f"VAST_TCP_PORT_{local}") if provider == "vast" else None
        known = (previous.get(key + "_public_port") if same_network
                 and previous.get(key + "_port") == local else None)
        cfg[key + "_public_port"] = port(ask(f"PUBLIC TCP port forwarded to VM port {local}",
                                               option or detected or known,
                                               local if provider == "direct" else None))
    if args.rent_mappings:
        pairs = mappings(args.rent_mappings)
    else:
        old_pairs = mappings(old["RENTING_PORT_MAPPINGS"]) if old.get("RENTING_PORT_MAPPINGS") else []
        local_ports = (expand_ports(args.rent_ports) if args.rent_ports
                       else [p[0] for p in old_pairs] if old_pairs
                       else expand_ports(old.get("RENTING_PORT_RANGE") or "40000-40009"))
        detected = {p: provider_env.get(f"VAST_TCP_PORT_{p}") for p in local_ports} if provider == "vast" else {}
        if provider == "direct":
            pairs = [[p, p] for p in local_ports]
        elif detected and all(detected.values()):
            pairs = [[p, port(detected[p])] for p in local_ports]
        elif (same_network and not any(detected.values()) and not args.rent_ports
              and [p[0] for p in previous.get("rent_mappings", [])] == local_ports):
            pairs = previous["rent_mappings"]
        else:
            say("Public rental mappings are required. Read IP & Port Info; do not guess them.")
            say("Each pair is [port inside VM, assigned public TCP port]. At least 3 pairs.")
            pairs = mappings(ask("Rental mappings JSON", None))
    # Validate detected/saved pairs too, including duplicate public allocations.
    pairs = mappings(json.dumps(pairs))
    local_set, public_set = {p[0] for p in pairs}, {p[1] for p in pairs}
    if min(local_set | {cfg['api_port'], cfg['ssh_port']}) < 1024:
        raise Blocked("Use unprivileged VM ports >=1024; preserve administration SSH port 22.")
    if cfg["api_port"] == cfg["ssh_port"] or cfg["api_public_port"] == cfg["ssh_public_port"]:
        raise Blocked("API and SSH must use different ports.")
    if local_set & {cfg["api_port"], cfg["ssh_port"]} or public_set & {cfg["api_public_port"], cfg["ssh_public_port"]}:
        raise Blocked("Rental ports overlap API/SSH ports.")
    if provider == "direct" and (any(a != b for a, b in pairs)
            or cfg["api_port"] != cfg["api_public_port"] or cfg["ssh_port"] != cfg["ssh_public_port"]):
        raise Blocked("Different public/internal ports require --provider nat or --provider vast.")
    if provider == "vast" and len(pairs) > 60:
        raise Blocked("Vast has a 64-port total limit. Reserve space for SSH, UDP, and executor services.")
    if provider == "vast" and provider_env.get("VAST_TCP_PORT_22"):
        admin_port = port(provider_env["VAST_TCP_PORT_22"])
        if admin_port in public_set | {cfg["api_public_port"], cfg["ssh_public_port"]}:
            raise Blocked(f"Public port {admin_port} forwards to administration SSH (VM port 22). "
                          "Use a separate mapping for executor SSH/API/rentals.")
    cfg["rent_mappings"] = pairs
    cfg["public_reachability"] = "UNVERIFIED: run probe from another computer"
    return cfg


def env_values(cfg):
    pairs = cfg["rent_mappings"]
    same = all(a == b for a, b in pairs)
    return {"INTERNAL_PORT": str(cfg["internal_port"]),
            "EXTERNAL_PORT": str(cfg["api_port"]), "SSH_PORT": str(cfg["ssh_port"]),
            "SSH_PUBLIC_PORT": str(cfg["ssh_public_port"]),
            "MINER_HOTKEY_SS58_ADDRESS": cfg["hotkey"],
            "RENTING_PORT_RANGE": ",".join(str(a) for a, _ in pairs) if same else "",
            "RENTING_PORT_MAPPINGS": "" if same else json.dumps(pairs, separators=(",", ":"))}


def render_env(text, values):
    result, remaining = [], dict(values)
    for line in text.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", line)
        if match and match[1] in values:
            if match[1] in remaining:
                key = match[1]
                result.append(key + "=" + json.dumps(remaining.pop(key)))
        else:
            result.append(line)
    result += [k + "=" + json.dumps(v) for k, v in remaining.items()]
    return "\n".join(result) + "\n"


def write_file(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise Blocked(f"Refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".lium-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def backup(path):
    path = Path(path)
    if path.exists():
        target = Path("/var/backups/lium-ubuntu-installer") / STAMP / str(path).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)
        say(f"Backup: {target}")


def fetch_checked(name, destination):
    with urllib.request.urlopen(UPSTREAM + name, timeout=60) as response:
        data = response.read(2_000_000)
    if hashlib.sha256(data).hexdigest() != HASHES[name]:
        raise Blocked(f"Integrity check failed for {name}. Nothing from that download was executed.")
    path = destination / name
    path.write_bytes(data)
    return path


def host_preflight():
    os_info = dotenv(Path("/etc/os-release"))
    if os_info.get("ID") != "ubuntu" or os_info.get("VERSION_ID") not in {"22.04", "24.04", "26.04"}:
        raise Blocked("This installer supports Ubuntu 22.04, 24.04 and 26.04 only.")
    if os.uname().machine != "x86_64":
        raise Blocked("The reviewed Sysbox package requires x86_64/amd64.")
    container = quiet(["systemd-detect-virt", "--container"])
    if container or Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        raise Blocked("This is a container. Use a full GPU VM or bare-metal host, e.g. Vast Ubuntu VM.")
    if not Path("/run/systemd/system").is_dir():
        raise Blocked("A host systemd service manager is required.")
    if version_tuple(os.uname().release) < (5, 19, 0):
        raise Blocked("Kernel >=5.19 is required. Install your provider-supported newer kernel and reboot first.")
    say(f"PASS Ubuntu {os_info['VERSION_ID']}, x86_64, kernel {os.uname().release}")
    return os_info


def containers():
    if not shutil.which("docker"):
        return []
    result = docker("ps", "-a", "--format", "{{.Names}}", capture=True)
    return [line for line in result.stdout.splitlines() if line]


def protect_workloads():
    names = containers()
    unsafe = [name for name in names if name not in KNOWN]
    if unsafe:
        raise Blocked("Install stopped: rental, validation, or unrelated containers exist:\n  "
                      + "\n  ".join(unsafe) + "\nNo containers were deleted. Finish rentals/jobs first; use check/diagnose meanwhile.")
    return names


def apt_install(packages):
    run([*APT, "install", "-y", "--no-install-recommends", *packages], timeout=1800)


def apt_directory(path):
    # _apt must traverse repository/key directories even with our private umask.
    path = root_directory(path)
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)


def ensure_driver(args):
    result = quiet(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    if result and all(re.fullmatch(r"\d+(?:\.\d+)+", x.strip()) for x in result.splitlines()):
        if min(version_tuple(x) for x in result.splitlines()) < (580, 65, 6):
            say("WARNING Driver is below the reviewed Lium idle-incentive minimum 580.65.06. "
                "It is preserved; arrange a provider-supported upgrade before expecting idle rewards.")
        say("PASS Existing NVIDIA driver: " + result.splitlines()[0] + "; preserving it.")
        return
    if shutil.which("nvidia-smi") or Path("/proc/driver/nvidia").exists():
        raise Blocked("NVIDIA driver exists but nvidia-smi failed. Check nvidia-smi directly; "
                      "driver/library mismatch often needs a reboot. This script will not reinstall over it.")
    if not args.install_driver:
        raise Blocked("NVIDIA driver is missing. Ask the GPU provider to install it, or rerun install "
                      "with --install-driver on a host where you manage the driver. A reboot will be needed.")
    if not any(p.read_text().strip() == "0x10de" for p in Path("/sys/bus/pci/devices").glob("*/vendor")):
        raise Blocked("No NVIDIA PCI device found. Check GPU passthrough with your provider first.")
    if shutil.which("docker") and containers():
        raise Blocked("Driver installation requires an empty host; existing containers were preserved.")
    run([*APT, "update"], timeout=600)
    apt_install(["ubuntu-drivers-common", "linux-headers-" + os.uname().release])
    run(["ubuntu-drivers", "install"], timeout=2400)
    say("NVIDIA driver installation finished. Reboot the VM yourself, then rerun the same installer.")
    raise SystemExit(194)


def ensure_docker(os_info):
    if shutil.which("docker"):
        # Never silently start a stopped daemon and resume unknown workloads.
        docker("info", capture=True)
        say("PASS Existing Docker daemon: " + dq("version", "--format", "{{.Server.Version}}"))
        if not dq("compose", "version", "--short"):
            installed = quiet(["dpkg-query", "-W", "-f=${Status}", "docker.io"])
            apt_install(["docker-compose-v2" if "install ok installed" in installed else "docker-compose-plugin"])
        return
    conflicts = []
    for name in ("docker.io", "podman-docker", "containerd", "runc", "docker-compose"):
        if "install ok installed" in quiet(["dpkg-query", "-W", "-f=${Status}", name]):
            conflicts.append(name)
    if conflicts:
        raise Blocked("Conflicting packages need manual review; not removing: " + ", ".join(conflicts))
    codename = os_info.get("UBUNTU_CODENAME") or os_info.get("VERSION_CODENAME")
    if codename not in {"jammy", "noble", "resolute"}:
        raise Blocked("Unsupported Ubuntu codename for Docker repository.")
    sources = list(Path("/etc/apt/sources.list.d").glob("*")) + [Path("/etc/apt/sources.list")]
    if not any(p.is_file() and "download.docker.com/linux/ubuntu" in p.read_text(errors="replace") for p in sources):
        key = Path("/etc/apt/keyrings/docker.asc")
        apt_directory(key.parent)
        apt_directory(Path("/etc/apt/sources.list.d"))
        with urllib.request.urlopen("https://download.docker.com/linux/ubuntu/gpg", timeout=30) as response:
            write_file(key, response.read().decode(), 0o644)
        write_file("/etc/apt/sources.list.d/lium-docker.sources",
                   f"Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: {codename}\n"
                   "Components: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n", 0o644)
    run([*APT, "update"], timeout=600)
    apt_install(["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"])
    run(["systemctl", "enable", "--now", "docker"], timeout=120)
    docker("info", capture=True)


def check_port_owners(cfg):
    owned_ports = set()
    if dq("inspect", EXECUTOR, "--format", "{{.Id}}"):
        data = json.loads(docker("inspect", EXECUTOR, capture=True).stdout)[0]
        published = data.get("NetworkSettings", {}).get("Ports") or {}
        for target, local in ((cfg.get("internal_port", cfg["api_port"]), cfg["api_port"]),
                              (22, cfg["ssh_port"])):
            if any(int(b["HostPort"]) == local for b in published.get(f"{target}/tcp") or []):
                owned_ports.add(local)
    rental_ports = {p[0] for p in cfg.get("rent_mappings", [])}
    for p in sorted({cfg["api_port"], cfg["ssh_port"]} | rental_ports):
        with socket.socket() as test:
            try:
                test.bind(("0.0.0.0", p))
            except OSError as e:
                if p not in owned_ports:
                    raise Blocked(f"VM TCP port {p} is occupied by another process. Select another port.") from e


def modules():
    needed = [m for m in ("ip_tables", "iptable_nat", "iptable_filter")
              if not Path("/sys/module", m).exists()]
    if needed:
        run(["modprobe", "-a", *needed])
    text = "ip_tables\niptable_nat\niptable_filter\n"
    path = Path("/etc/modules-load.d/lium-iptables.conf")
    if not path.exists() or path.read_text() != text:
        backup(path)
        write_file(path, text, 0o644)
    say("PASS Required iptables modules loaded. No firewall flush or nft/legacy switch performed.")


def gpu_probe(runtime):
    probe_id = uuid.uuid4().hex
    name = "lium-installer-probe-" + probe_id
    try:
        args = ["run", "--rm", "--name", name, "--label", "io.lium.installer.probe=true",
                "--label", "io.lium.installer.run=" + probe_id,
                "--runtime=" + runtime, "--gpus", "all", "--entrypoint", "nvidia-smi",
                PROBE_IMAGE, "--query-gpu=uuid", "--format=csv,noheader"]
        result = docker(*args, capture=True, check=False, timeout=120)
        if result.returncode:
            say("GPU container probe failed:\n" + (result.stderr or result.stdout)[-5000:])
            return False
        expected = set(quiet(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"]).splitlines())
        actual = set(result.stdout.strip().splitlines())
        if not expected or expected != actual:
            say("GPU container visibility differs from host GPU UUIDs.")
            return False
        say(f"PASS {runtime}: all {len(expected)} GPU(s) visible inside a container.")
        return True
    finally:
        # Only the specifically named disposable probe created by this run.
        if dq("inspect", name, "--format", '{{index .Config.Labels "io.lium.installer.run"}}') == probe_id:
            docker("rm", "-f", name, check=False, timeout=30)


def ensure_runtime(cfg, upstream_dir):
    runtime_info = dq("info", "--format", "{{json .Runtimes}}")
    sysbox_version = quiet(["sysbox-runc", "--version"])
    match = re.search(r"version:\s*(\d+\.\d+\.\d+)", sysbox_version)
    installed = bool(match and version_tuple(match[1]) >= (0, 7, 1)
                     and "sysbox-runc" in runtime_info and "nvidia" in runtime_info)
    if not dq("image", "inspect", PROBE_IMAGE, "--format", "{{.Id}}"):
        docker("pull", PROBE_IMAGE, timeout=1800)
    if installed and gpu_probe("sysbox-runc") and gpu_probe("nvidia"):
        say("PASS Sysbox/NVIDIA runtime already works; skipping runtime installation.")
        return
    names = containers()
    if names:
        raise Blocked("Runtime installation/repair requires zero Docker containers, including stopped ones. "
                      "The upstream installer removes containers; this wrapper refuses that operation. "
                      "Preserve existing workloads and arrange maintenance first. Containers: " + ", ".join(names))
    backup("/etc/docker/daemon.json")
    backup("/etc/nvidia-container-runtime/config.toml")
    run(["env", f"EXECUTOR_PORT={cfg['api_port']}", f"SSH_PORT={cfg['ssh_port']}",
         "bash", str(upstream_dir / "nvidia_docker_sysbox_setup.sh")],
        cwd=upstream_dir, timeout=2400)
    if not gpu_probe("nvidia") or not gpu_probe("sysbox-runc"):
        raise Blocked("Runtime installed but GPU container test failed. See logs and diagnose.")


def disk_check():
    data_root = Path(dq("info", "--format", "{{.DockerRootDir}}") or "/var/lib/docker")
    usage = shutil.disk_usage(data_root)
    memories = quiet(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    vram = sum(float(x.strip()) for x in memories.splitlines() if x.strip()) * 1024**2
    say(f"Docker storage {data_root}: {usage.total/1e9:.1f} GB total; {usage.free/1e9:.1f} GB free.")
    if usage.free < 50 * 1024**3 or usage.used / usage.total > 0.9:
        raise Blocked("Insufficient Docker storage headroom: need at least 50 GiB free and <=90% used "
                      "(installer uses a conservative free-space threshold). No data was removed.")
    if usage.total < 1.5 * vram:
        say("WARNING Total disk is below 1.5x GPU VRAM; reviewed Lium rules withhold idle incentives.")
    fs_type = quiet(["findmnt", "-n", "-o", "FSTYPE", "-T", str(data_root)])
    say(f"Docker backing filesystem: {fs_type or 'unknown'}. GPU splitting storage migration is not automated.")


def firewall(cfg, modify=False):
    ports = sorted({cfg["api_port"], cfg["ssh_port"], *[p[0] for p in cfg["rent_mappings"]]})
    status = quiet(["ufw", "status"])
    if "Status: active" in status:
        if modify:
            for p in ports:
                run(["ufw", "allow", str(p) + "/tcp", "comment", "lium-executor"], timeout=30)
        else:
            say("WARNING UFW is active. Review rules for VM TCP ports " + ",".join(map(str, ports))
                + "; --allow-ufw adds specific rules without disabling/enabling UFW.")
    elif modify:
        say("UFW is absent/inactive; left that way. Check provider firewall and other host rules.")
    say("Public/provider forwarding is separate. Docker publication may bypass UFW; "
        "this script does not change DOCKER-USER or other nftables/iptables policy.")


def http_json(ip, p, route, timeout=10):
    url = f"http://{ip}:{port(p)}/{route}"
    # A proxy could make a local health check test a different endpoint.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        data = response.read(1_000_000)
    return json.loads(data)


def ssh_banner(ip, p):
    deadline = time.monotonic() + 5
    with socket.create_connection((ip, port(p)), timeout=5) as stream:
        data = b""
        while len(data) < 4096 and time.monotonic() < deadline:
            stream.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = stream.recv(min(512, 4096 - len(data)))
            if not chunk:
                break
            data += chunk
            # TCP may split the identification across packets; servers may also
            # send informational lines before the SSH identification line.
            for line in data.split(b"\n")[:-1]:
                if line.startswith(b"SSH-"):
                    return line.rstrip(b"\r").decode("ascii", "replace")
    raise Blocked(f"TCP {p} answered but did not return a complete SSH banner.")


def running_configuration(cfg):
    for name in sorted(KNOWN):
        if dq("inspect", name, "--format", "{{.State.Running}}") != "true":
            raise Blocked(f"Lium service is not running: {name}")
    raw = dq("inspect", EXECUTOR, "--format", "{{json .Config.Env}}")
    values = dict(item.split("=", 1) for item in json.loads(raw or "[]") if "=" in item)
    different = [key for key, value in env_values(cfg).items() if values.get(key, "") != value]
    if different:
        raise Blocked("Running executor configuration differs from saved settings: " + ", ".join(different)
                      + ". Rerun install during idle maintenance to finish applying the configuration.")


def local_health(cfg, wait=0):
    deadline = time.monotonic() + wait
    last_progress = 0
    while True:
        try:
            info = http_json("127.0.0.1", cfg["api_port"], "version", timeout=5)
            if not isinstance(info, dict) or not info.get("version"):
                raise Blocked("Local API response is not Lium's version JSON.")
            banner = ssh_banner("127.0.0.1", cfg["ssh_port"])
            state = dq("inspect", EXECUTOR, "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}")
            if state != "healthy":
                raise Blocked("Executor health is " + (state or "not available"))
            running_configuration(cfg)
            say("PASS Local executor API: " + json.dumps(info))
            say("PASS Local executor SSH: " + banner)
            break
        except (OSError, ValueError, Blocked) as e:
            if time.monotonic() >= deadline:
                raise Blocked(f"Local executor health failed: {e}. Run diagnose; check cloud ports separately.") from e
            if time.monotonic() - last_progress > 30:
                say("Waiting for executor startup: " + str(e))
                last_progress = time.monotonic()
            time.sleep(3)
    inside = dq("exec", EXECUTOR, "nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader")
    host = quiet(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"])
    if not inside or set(inside.splitlines()) != set(host.splitlines()):
        raise Blocked("The running executor does not see the same GPUs as the host.")
    try:
        status = http_json("127.0.0.1", cfg["api_port"], "update-status")
        runner = status.get("runner", {})
        say("Updater: " + json.dumps({k: runner.get(k) for k in
                                      ("running_digest", "expected_digest", "update_pending", "error")}))
        if runner.get("update_pending") is not False:
            say("WARNING Updater has not confirmed the current runner digest. Wait one update cycle and rerun check.")
    except (OSError, ValueError, AttributeError) as e:
        say("WARNING Could not read updater status: " + str(e))


def endpoint_report(cfg):
    text = ["LIUM NODE: LOCAL INSTALLATION / CONNECTIVITY REPORT",
            "Public reachability and Lium validation: NOT VERIFIED by local install.",
            "", "Provider portal > Add Node > Enter the details:",
            "  Machine IP: " + cfg["public_ip"], "  Port: " + str(cfg["api_public_port"]),
            "  Miner public hotkey: " + cfg["hotkey"],
            "  GPU model/count: use the actual nvidia-smi output below; choose price in the portal.",
            quiet(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]),
            "", f"Public {cfg['api_public_port']}/tcp -> VM {cfg['api_port']}/tcp -> executor API",
            f"Public {cfg['ssh_public_port']}/tcp -> VM {cfg['ssh_port']}/tcp -> executor SSH",
            "Rental [VM port, public port] mappings: " + json.dumps(cfg["rent_mappings"]),
            "", "RUN FROM YOUR MAC / ANOTHER NETWORK, not from this GPU VM:",
            f"curl --connect-timeout 5 --max-time 10 http://{cfg['public_ip']}:{cfg['api_public_port']}/version",
            f"nc -vz {cfg['public_ip']} {cfg['ssh_public_port']}",
            "", "A public SSH TCP connection alone does not prove validator authentication.",
            "Rental ports may have no listener until a test/rental starts. Let Lium check them.",
            "Use the public mapped port in the portal, not the local port or VM administration SSH.",
            "The same registered hotkey can be used; no Bittensor registration fee is paid here.",
            "If the old node has the wrong IP/port, follow Lium's node replacement flow;",
            "the installer does not delete it. Finish any rentals before changing a listing.",
            "Central Miner Server must be enabled if you are not running a separate miner.",
            "", "Local paths:", "  Executor directory: " + cfg["directory"],
            "  Configuration: " + str(Path(cfg["directory"]) / ".env"),
            "  Run log: " + str(LOG_PATH),
            "  Reviewed upstream commit: " + COMMIT]
    report = "\n".join(text) + "\n"
    write_file(STATE_DIR / "node-report.txt", report)
    say(report)


def install(args):
    os_info = host_preflight()
    if shutil.which("docker"):
        protect_workloads()
    directory = find_directory(args.directory)
    cfg = resolve_configuration(args, directory)
    say("Configuration (public information only):\n" + json.dumps(cfg, indent=2))
    old = dotenv(directory / ".env")
    changed = any(old.get(k, "") != v for k, v in env_values(cfg).items())
    existing = bool(dq("inspect", RUNNER, "--format", "{{.Id}}"))
    if existing and changed and not args.reconfigure:
        raise Blocked("Existing Lium configuration would change. Review the printed values, then rerun "
                      "with --reconfigure during idle maintenance. Current configuration is preserved.")
    ensure_driver(args)
    run([*APT, "update"], timeout=600)
    apt_install(["ca-certificates", "curl", "wget", "gnupg", "jq", "iproute2", "iptables", "kmod", "pciutils"])
    ensure_docker(os_info)
    protect_workloads()
    check_port_owners(cfg)
    disk_check()
    modules()
    if quiet(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]) != "yes":
        say("WARNING Clock is not reported as NTP-synchronized. Correct time sync before validator checks.")
    firewall(cfg, args.allow_ufw)
    with tempfile.TemporaryDirectory(prefix="lium-reviewed-") as temporary:
        upstream_dir = Path(temporary)
        for name in HASHES:
            fetch_checked(name, upstream_dir)
        ensure_runtime(cfg, upstream_dir)
        protect_workloads()
        directory.mkdir(parents=True, exist_ok=True)
        env_path, compose_path = directory / ".env", directory / "docker-compose.yml"
        if env_path.is_symlink() or compose_path.is_symlink():
            raise Blocked("Configuration symlinks are not supported.")
        base = env_path.read_text() if env_path.exists() else (upstream_dir / ".env.template").read_text()
        # Avoid replacing a bind-mounted file just to change its quoting; the
        # running container would retain the old inode until it was recreated.
        new_env = base if env_path.exists() and not changed else render_env(base, env_values(cfg))
        new_compose = (upstream_dir / "docker-compose.yml").read_text()
        compose_changed = not compose_path.exists() or compose_path.read_text() != new_compose
        if existing and compose_changed and not args.reconfigure:
            raise Blocked("Existing compose file differs from the reviewed file. Rerun with --reconfigure "
                          "during maintenance to back it up and adopt the reviewed deployment.")
        pending = STATE_DIR / "deployment-pending.json"
        recreate = not existing or changed or compose_changed or args.reconfigure or pending.exists()
        # Checkpoint BEFORE replacing a bind-mounted file. If interrupted, the next
        # run must recreate the runner even when the on-disk .env already matches.
        if recreate:
            write_file(pending, json.dumps({"directory": str(directory), "started": STAMP}) + "\n")
        write_file(STATE_DIR / "state.json", json.dumps(cfg, indent=2) + "\n")
        for path, text, mode in ((env_path, new_env, 0o600), (compose_path, new_compose, 0o644)):
            if not path.exists() or path.read_text() != text:
                backup(path)
                write_file(path, text, mode)
        # Official compose references $HOME. Use the target root account's home,
        # not a caller's preserved HOME, to avoid mounting the wrong wallet path.
        import pwd
        root_home = pwd.getpwuid(0).pw_dir
        (Path(root_home) / ".bittensor/wallets").mkdir(parents=True, exist_ok=True)
        clean_env = ["env"]
        for key in [*env_values(cfg), "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"]:
            clean_env += ["-u", key]
        prefix = [*clean_env, "HOME=" + root_home, "docker", "--host", "unix:///var/run/docker.sock",
                  "compose", "--project-name", "executor", "--env-file", str(env_path), "-f", str(compose_path)]
        run([*prefix, "config", "--quiet"], cwd=directory)
        protect_workloads()
        # Recreating the runner is necessary to load a replaced bind-mounted .env.
        if recreate:
            run([*prefix, "pull"], cwd=directory, timeout=1800)
            # Pulls can take minutes. Recheck immediately before changing containers.
            protect_workloads()
            check_port_owners(cfg)
            run([*prefix, "up", "-d", "--force-recreate"], cwd=directory, timeout=300)
        else:
            run([*prefix, "up", "-d"], cwd=directory, timeout=300)
    local_health(cfg, wait=args.wait_seconds)
    disk_check()
    (STATE_DIR / "deployment-pending.json").unlink(missing_ok=True)
    endpoint_report(cfg)
    say("LOCAL INSTALL COMPLETE. Public networking and Lium validator acceptance still require verification.")


def load_state():
    path = STATE_DIR / "state.json"
    if not path.exists():
        raise Blocked("No saved installer configuration. Use diagnose for an older installation, or run install.")
    return saved_record("state.json")


def check():
    cfg = load_state()
    if (STATE_DIR / "deployment-pending.json").exists():
        raise Blocked("An installation is incomplete. Run diagnose, then rerun install during idle "
                      "maintenance; a healthy old container does not confirm the new settings.")
    host_preflight()
    for module in ("ip_tables", "iptable_nat", "iptable_filter"):
        if not Path("/sys/module", module).exists():
            raise Blocked(f"Kernel module {module} is absent. Rerun install to load it.")
    if not quiet(["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"]):
        raise Blocked("nvidia-smi failed. Run it directly to see the driver error.")
    docker("info", capture=True)
    disk_check()
    firewall(cfg)
    local_health(cfg)
    endpoint_report(cfg)


def diagnose():
    say("Diagnostics: no package changes, restarts, downloads or container creation.")
    commands = [["uname", "-a"], ["nvidia-smi"], ["df", "-h", "/var/lib/docker"],
                ["ss", "-lnt"], ["iptables", "--version"], ["ufw", "status", "verbose"],
                ["timedatectl", "status"], ["sysbox-runc", "--version"],
                ["journalctl", "-u", "docker", "-u", "sysbox-mgr", "-u", "sysbox-fs",
                 "--no-pager", "-n", "100"]]
    for cmd in commands:
        if shutil.which(cmd[0]):
            try:
                run(cmd, check=False, timeout=30)
            except subprocess.TimeoutExpired:
                say("Diagnostic timed out: " + cmd[0])
    if shutil.which("docker"):
        docker("ps", "-a", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}", check=False)
        for name in sorted(KNOWN):
            if dq("inspect", name, "--format", "{{.Id}}"):
                try:
                    docker("logs", "--since", "30m", "--tail", "120", "--timestamps", name, check=False, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as e:
                    say(f"Could not collect logs for {name}: {e}")
    say("Diagnostics saved to " + str(LOG_PATH) + ". Review logs before sharing; workload output may be present.")


def probe(args):
    ip = public_ip(args.public_ip)
    api, ssh = port(args.api_public_port), port(args.ssh_public_port)
    info = http_json(ip, api, "version")
    if not isinstance(info, dict) or not info.get("version"):
        raise Blocked("The public API did not return Lium version JSON.")
    say("PASS Public API: " + json.dumps(info))
    say("PASS Public SSH banner: " + ssh_banner(ip, ssh))
    say("Reachability passed from THIS computer. This does not verify validator authentication, rental ports, "
        "GPU performance, or reward eligibility. Running this on the GPU VM is not an outside test.")


def parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    ins = sub.add_parser("install", help="Install missing prerequisites and official Lium stack; interactive by default")
    ins.add_argument("--provider", choices=["direct", "vast", "nat"], help="direct=matching public/VM ports; vast/nat=explicit mappings")
    ins.add_argument("--hotkey", help="Registered PUBLIC miner SS58 address, never a private key")
    ins.add_argument("--public-ip", help="IPv4 advertised to Lium; use provider panel's public IP")
    ins.add_argument("--api-port", type=port, help="VM port Docker publishes (default 8080, or existing value)")
    ins.add_argument("--ssh-port", type=port, help="VM port Docker publishes for executor SSH (default 2200)")
    ins.add_argument("--api-public-port", type=port, help="Public TCP port forwarded to the VM API port")
    ins.add_argument("--ssh-public-port", type=port, help="Public TCP port forwarded to the VM executor SSH port; not administration SSH")
    rents = ins.add_mutually_exclusive_group()
    rents.add_argument("--rent-ports", help="VM rental ports, e.g. 40000-40009; Vast needs corresponding mappings")
    rents.add_argument("--rent-mappings", help="JSON [VM port, public port] pairs, at least 3; quote the argument")
    ins.add_argument("--directory", help="Absolute executor directory; existing runner mount is detected automatically")
    ins.add_argument("--non-interactive", action="store_true", help="Fail on missing input rather than prompt")
    ins.add_argument("--reconfigure", action="store_true", help="Back up and change an existing idle Lium stack, recreating its runner")
    ins.add_argument("--install-driver", action="store_true", help="If no driver exists, install Ubuntu's recommended driver, then stop for reboot")
    ins.add_argument("--allow-ufw", action="store_true", help="Add specific TCP allow rules only if UFW is already active")
    ins.add_argument("--wait-seconds", type=int, default=900, help="Maximum initial executor health wait (default 900)")
    sub.add_parser("check", help="Read local health/configuration; no package or runtime changes")
    sub.add_parser("diagnose", help="Collect bounded host and known Lium container logs; no full environment dumps")
    pr = sub.add_parser("probe", help="Run on ANOTHER computer to test your public API and SSH")
    pr.add_argument("--public-ip", required=True)
    pr.add_argument("--api-public-port", required=True, type=port)
    pr.add_argument("--ssh-public-port", required=True, type=port)
    return ap


def main():
    global LOG, LOG_PATH
    args = parser().parse_args()
    if args.command == "probe":
        probe(args)
        return
    if os.geteuid() != 0:
        raise Blocked("Run install/check/diagnose with sudo on the GPU Ubuntu host.")
    # Use the local Docker daemon and avoid apt restarting unrelated services.
    # Network downloads still honor urllib's usual TLS validation.
    os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "SYSBOX_SETUP_HOST_ROOT",
                "SYSBOX_SETUP_LIB", "SYSBOX_SKIP_KERNEL_CHECK", "APT_ROOT"):
        os.environ.pop(key, None)
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    os.environ["NEEDRESTART_MODE"] = "l"
    os.umask(0o077)
    root_directory(STATE_DIR)
    root_directory(LOG_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH = LOG_DIR / (STAMP + "-" + args.command + ".log")
    LOG = LOG_PATH.open("x", buffering=1)
    with (STATE_DIR / "installer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Blocked("Another installer/check is running. Wait for it to finish.") from e
        say(f"Lium Ubuntu installer {VERSION}; reviewed {COMMIT}; log {LOG_PATH}")
        if args.command == "install":
            if not 30 <= args.wait_seconds <= 3600:
                raise Blocked("--wait-seconds must be between 30 and 3600.")
            install(args)
        elif args.command == "check":
            check()
        else:
            diagnose()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("Interrupted. Completed package changes remain; rerun after reviewing the log.")
        sys.exit(130)
    except (Blocked, OSError, ValueError, subprocess.SubprocessError) as error:
        say("FAILED: " + str(error))
        if LOG_PATH:
            say("Run log: " + str(LOG_PATH))
            say("Diagnostics: sudo python3 lium_ubuntu_installer.py diagnose")
        sys.exit(1)
