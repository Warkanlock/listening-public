"""Print Compose's private Listening bind addresses for this Debian host."""

import argparse
import ipaddress
import json
import subprocess
import sys


def addresses(interface):
    info = json.loads(subprocess.check_output(
        ["ip", "-j", "-4", "addr", "show", "dev", interface], text=True))
    private = [ipaddress.ip_network(cidr) for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    lan = [entry["local"] for link in info for entry in link["addr_info"]
           if entry["scope"] == "global" and any(ipaddress.ip_address(entry["local"]) in net for net in private)]
    if len(lan) != 1:
        raise ValueError("Choose the wired interface with exactly one home LAN IPv4 address.")
    tail = ipaddress.ip_address(subprocess.check_output(["tailscale", "ip", "-4"], text=True).strip())
    if tail not in ipaddress.ip_network("100.64.0.0/10"):
        raise ValueError("Connect the server to its tailnet before publishing Listening.")
    return lan[0], tail


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("interface", help="wired LAN interface from ip -br addr")
    try:
        lan, tail = addresses(parser.parse_args().interface)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
    print(f"LISTENING_LAN_BIND={lan}\nLISTENING_TAILNET_BIND={tail}")
