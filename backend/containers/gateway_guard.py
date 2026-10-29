"""Trusted container init and independently repeatable kernel-policy inspection."""

import ipaddress
import os
import re
import shlex
import subprocess
import sys

POLICY = "public-http-v1"
CHAIN = "TRELLIS_EGRESS"
_IPV4_BLOCKS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "168.63.129.16/32",
)
_IPV6_BLOCKS = (
    "::/96",
    "::ffff:0:0/96",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "2001::/23",
    "2001:db8::/32",
    "2002::/16",
    "3fff::/20",
)


def rules(ipv6: bool) -> list[tuple[str, ...]]:
    result = [("-A", CHAIN, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")]
    if not ipv6:
        ports = docker_dns_ports()
        for protocol in ("udp", "tcp"):
            result.append(
                (
                    "-A",
                    CHAIN,
                    "-d",
                    "127.0.0.11/32",
                    "-p",
                    protocol,
                    "-m",
                    "owner",
                    "--uid-owner",
                    "10001",
                    "-m",
                    protocol,
                    "--dport",
                    str(ports[protocol]),
                    "-m",
                    "conntrack",
                    "--ctorigdst",
                    "127.0.0.11",
                    "--ctorigdstport",
                    "53",
                    "-j",
                    "ACCEPT",
                )
            )
    for address in _IPV6_BLOCKS if ipv6 else _IPV4_BLOCKS:
        result.append(("-A", CHAIN, "-d", address, "-j", "DROP"))
    for port in ("80", "443"):
        rule = ("-A", CHAIN)
        if ipv6:
            rule += ("-d", "2000::/3")
        result.append(
            (
                *rule,
                "-p",
                "tcp",
                "-m",
                "owner",
                "--uid-owner",
                "10001",
                "-m",
                "tcp",
                "--dport",
                port,
                "-j",
                "ACCEPT",
            )
        )
    result.append(("-A", CHAIN, "-j", "DROP"))
    return result


def run(binary: str, *args: str) -> str:
    return subprocess.run(
        (binary, "--wait", "2", *args), check=True, capture_output=True, text=True, timeout=5
    ).stdout


def docker_dns_ports() -> dict[str, int]:
    ports = {}
    for line in run("iptables", "-t", "nat", "-S", "DOCKER_OUTPUT").splitlines():
        parts = shlex.split(line)
        for protocol in ("udp", "tcp"):
            prefix = [
                "-A",
                "DOCKER_OUTPUT",
                "-d",
                "127.0.0.11/32",
                "-p",
                protocol,
                "-m",
                protocol,
                "--dport",
                "53",
                "-j",
                "DNAT",
                "--to-destination",
            ]
            if parts[:-1] == prefix:
                match = re.fullmatch(r"127\.0\.0\.11:([0-9]{1,5})", parts[-1])
                if match and 1 <= int(match[1]) <= 65535:
                    if protocol in ports:
                        raise RuntimeError("ambiguous Docker DNS NAT mapping")
                    ports[protocol] = int(match[1])
    if set(ports) != {"tcp", "udp"}:
        raise RuntimeError("gateway requires a user-defined Docker network with embedded DNS")
    return ports


def normalized(rule: tuple[str, ...]) -> tuple[str, ...]:
    parts = list(rule)
    if "-d" in parts:
        index = parts.index("-d") + 1
        parts[index] = str(ipaddress.ip_network(parts[index]))
    if "--ctstate" in parts:
        index = parts.index("--ctstate") + 1
        parts[index] = ",".join(sorted(parts[index].split(",")))
    return tuple(parts)


def install() -> None:
    for binary, ipv6 in (("iptables", False), ("ip6tables", True)):
        run(binary, "-P", "OUTPUT", "DROP")
        run(binary, "-N", CHAIN)
        for rule in rules(ipv6):
            run(binary, *rule)
        run(binary, "-A", "OUTPUT", "-j", CHAIN)
    check()


def check() -> None:
    for binary, ipv6 in (("iptables", False), ("ip6tables", True)):
        output = [tuple(shlex.split(line)) for line in run(binary, "-S", "OUTPUT").splitlines()]
        chain = [tuple(shlex.split(line)) for line in run(binary, "-S", CHAIN).splitlines()]
        if output != [("-P", "OUTPUT", "DROP"), ("-A", "OUTPUT", "-j", CHAIN)]:
            raise RuntimeError("unexpected gateway OUTPUT policy")
        expected = [("-N", CHAIN), *rules(ipv6)]
        if [normalized(rule) for rule in chain] != [normalized(rule) for rule in expected]:
            raise RuntimeError(f"unexpected gateway egress rules: {chain!r}; expected {expected!r}")


def check_process() -> None:
    with open("/proc/1/status") as status_file:
        status = dict(line.split(":", 1) for line in status_file if ":" in line)
    if (
        status.get("Uid", "").split() != ["10001"] * 4
        or any(int(status.get(key, "1").strip(), 16) for key in ("CapEff", "CapPrm", "CapBnd"))
        or status.get("NoNewPrivs", "").strip() != "1"
    ):
        raise RuntimeError("gateway process has not dropped privileges")


def main() -> None:
    if sys.argv[1:] == ["check"]:
        check()
        check_process()
        print(POLICY)
        return
    if sys.argv[1:] != ["init"]:
        raise SystemExit("expected init or check")
    install()
    os.chown("/run/trellis-egress", 0, 0)
    os.chmod("/run/trellis-egress", 0o750)
    os.chown("/run/trellis-egress", 10001, 10001)
    os.execvp(
        "setpriv",
        (
            "setpriv",
            "--reuid=10001",
            "--regid=10001",
            "--clear-groups",
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--no-new-privs",
            "python",
            "-m",
            "app.infrastructure.web_gateway",
            "--socket",
            "/run/trellis-egress/gateway.sock",
            "--admin-host",
            "0.0.0.0",
            "--admin-port",
            "8081",
            "--admin-secret-file",
            "/run/secrets/gateway-admin",
        ),
    )


if __name__ == "__main__":
    main()
