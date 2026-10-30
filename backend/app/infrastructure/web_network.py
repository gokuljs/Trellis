"""Fail-closed public destinations and DNS-pinned socket establishment."""

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import dns.asyncresolver
import dns.exception
import dns.resolver


class DestinationBlocked(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PublicURL:
    url: str
    scheme: str
    host: str
    port: int
    authority: str
    target: str
    path: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"


def public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address not in ipaddress.ip_network("2000::/3"):
            return False
        if address in ipaddress.ip_network("2001::/23"):
            return False
        # Embedded IPv4 routes and translation prefixes can reach non-public peers.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network(
            "64:ff9b:1::/48"
        ):
            return False
    elif address in ipaddress.ip_network("192.0.0.0/24") or address in ipaddress.ip_network(
        "192.88.99.0/24"
    ):
        return False
    return str(address) != "168.63.129.16"


def parse_public_url(value: str) -> PublicURL:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        raise DestinationBlocked("invalid_url")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise DestinationBlocked("invalid_authority") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or not host
        or "@" in parts.netloc
        or "%" in parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.netloc.endswith(":")
    ):
        raise DestinationBlocked("invalid_authority")
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if port not in {80, 443}:
        raise DestinationBlocked("blocked_port")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", host) or ":" in host:
            raise DestinationBlocked("ambiguous_host") from None
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise DestinationBlocked("invalid_host") from error
        if (
            len(host) > 253
            or host.endswith(".")
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
        ):
            raise DestinationBlocked("invalid_host") from None
        # inet_aton accepts octal, hexadecimal and single-integer IPv4 forms.
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise DestinationBlocked("ambiguous_host")
    else:
        if not public_address(str(literal)):
            raise DestinationBlocked("blocked_address")
        host = str(literal)
    bracketed_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    authority = bracketed_host if port == default_port else f"{bracketed_host}:{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    target = path + (f"?{query}" if query else "")
    canonical = urlunsplit((parts.scheme, authority, path, query, ""))
    if len(canonical) > 4096:
        raise DestinationBlocked("canonical_url_limit")
    return PublicURL(
        canonical,
        parts.scheme,
        host,
        port,
        authority,
        target,
        path,
    )


def validate_scope(url: PublicURL, origin: str | None, path_prefix: str) -> None:
    if origin is None:
        return
    scope = parse_public_url(origin)
    if scope.url != scope.origin + "/" or url.origin != scope.origin:
        raise DestinationBlocked("outside_navigation_scope")
    # Decode repeatedly so encoded separators and dot segments cannot escape scope.
    path = url.path
    for _ in range(4):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        raise DestinationBlocked("ambiguous_scope_path")
    if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise DestinationBlocked("ambiguous_scope_path")
    prefix = path_prefix.rstrip("/") or "/"
    if not (prefix == "/" or path == prefix or path.startswith(prefix + "/")):
        raise DestinationBlocked("outside_navigation_scope")


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> tuple[str, ...]: ...


class SystemResolver:
    def __init__(self, resolver: dns.asyncresolver.Resolver | None = None) -> None:
        self._resolver = resolver or dns.asyncresolver.Resolver()

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        try:
            return (str(ipaddress.ip_address(host)),)
        except ValueError:
            pass

        async def query(record_type: str) -> tuple[str, ...]:
            try:
                answer = await self._resolver.resolve(
                    host, record_type, lifetime=5, search=False, raise_on_no_answer=False
                )
                return tuple(str(record) for record in answer)
            except dns.resolver.NXDOMAIN, dns.resolver.NoAnswer:
                return ()

        tasks = [asyncio.create_task(query(kind)) for kind in ("A", "AAAA")]
        try:
            answers = await asyncio.gather(*tasks)
            return tuple(dict.fromkeys(address for answer in answers for address in answer))
        except dns.exception.Timeout as error:
            raise TimeoutError("DNS deadline") from error
        except dns.exception.DNSException as error:
            raise OSError("DNS unavailable") from error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class PublicConnector:
    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver or SystemResolver()

    async def connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Reuse URL parsing for CONNECT authorities, including literal policy.
        authority = f"[{host}]" if ":" in host else host
        parsed = parse_public_url(f"http://{authority}:{port}/")
        if parsed.host != host or parsed.port != port or parsed.target != "/":
            raise DestinationBlocked("ambiguous_connect_authority")
        addresses = await self._resolver.resolve(parsed.host, parsed.port)
        if not addresses:
            raise OSError("no DNS answers")
        if len(addresses) > 64 or any(not public_address(address) for address in addresses):
            raise DestinationBlocked("blocked_dns_answer")
        last_error: OSError | None = None
        for address in addresses:
            try:
                reader, writer = await asyncio.open_connection(
                    address,
                    port,
                    family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                    limit=65536,
                )
            except OSError as error:
                last_error = error
                continue
            peer = writer.get_extra_info("peername")
            if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(address):
                writer.close()
                await writer.wait_closed()
                raise DestinationBlocked("unexpected_socket_peer")
            return reader, writer
        raise last_error or OSError("no reachable public address")
