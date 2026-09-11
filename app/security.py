import urllib.parse
from typing import Set

ALLOWED_OLLAMA_HOSTS: Set[str] = {"127.0.0.1", "localhost", "::1"}
ALLOWED_OLLAMA_PORT: int = 11434

ALLOWED_BIND_HOSTS: Set[str] = {"127.0.0.1", "localhost", "::1"}

def validate_ollama_url(url: str, strict_mode: bool = True) -> str:
    """
    Validates and canonicalizes an Ollama base URL.

    Requirements in strict offline mode (strict_mode=True):
    - Scheme must be strictly 'http' (reject https and other schemes).
    - Host must be strictly in {'127.0.0.1', 'localhost', '::1'} (reject public/LAN IPs, 0.0.0.0, arbitrary hosts).
    - Port must be strictly 11434.
    - Reject URLs with credentials (user:pass@...).
    - Reject URLs containing paths that change the intended Ollama endpoint (only empty or '/' allowed).
    - Reject URLs with query parameters or fragments.
    - No reliance on string prefix checks like host.startswith('127.').

    Raises ValueError with a clear explanation if the URL is invalid.
    Returns canonical sanitized URL (without trailing slash).
    """
    if not url or not isinstance(url, str):
        raise ValueError("Ollama URL must be a non-empty string.")

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception as e:
        raise ValueError(f"Failed to parse Ollama URL '{clean_url}': {e}") from e

    # 1. Scheme check
    if not parsed.scheme:
        raise ValueError(f"Missing scheme in Ollama URL '{clean_url}'. Expected 'http://'.")
    if parsed.scheme.lower() != "http":
        raise ValueError(
            f"Invalid scheme '{parsed.scheme}' in Ollama URL '{clean_url}'. "
            "In strict offline mode, only 'http' is permitted (https and other schemes are prohibited)."
        )

    # 2. Credentials check
    if parsed.username or parsed.password or ("@" in (parsed.netloc or "")):
        raise ValueError("URLs containing credentials (user:password@...) are prohibited.")

    # 3. Path, query, fragment check
    # Only empty path or root '/' is allowed. Paths like '/api/remote' change the endpoint.
    if parsed.path and parsed.path != "/":
        raise ValueError(
            f"Invalid path '{parsed.path}' in Ollama URL. "
            "URLs containing paths that alter the base Ollama endpoint are not allowed."
        )
    if parsed.query:
        raise ValueError("Ollama base URL must not contain query parameters.")
    if parsed.fragment:
        raise ValueError("Ollama base URL must not contain URL fragments.")

    # 4. Hostname check
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Ollama URL '{clean_url}' does not contain a valid host.")

    hostname_lower = hostname.lower()

    if strict_mode:
        if hostname_lower not in ALLOWED_OLLAMA_HOSTS:
            raise ValueError(
                f"Host '{hostname}' is not permitted in strict offline mode. "
                f"Ollama connections are strictly restricted to local loopback ({', '.join(sorted(ALLOWED_OLLAMA_HOSTS))}). "
                "LAN, public, and wildcard addresses (e.g. 0.0.0.0) are rejected."
            )

        # 5. Port check
        if parsed.port is None:
            raise ValueError(
                f"Missing port in Ollama URL '{clean_url}'. "
                f"Port must be explicitly specified as {ALLOWED_OLLAMA_PORT}."
            )
        if parsed.port != ALLOWED_OLLAMA_PORT:
            raise ValueError(
                f"Invalid port '{parsed.port}' in Ollama URL. "
                f"In strict offline mode, only standard local Ollama port {ALLOWED_OLLAMA_PORT} is allowed."
            )

    # Return canonical sanitized URL without trailing slash
    if hostname_lower == "::1":
        return f"http://[::1]:{parsed.port}"
    return f"http://{hostname_lower}:{parsed.port}"


def validate_bind_host(host: str, strict_mode: bool = True) -> str:
    """
    Validates Flask server bind address.

    When strict_mode=True:
    - Only allows loopback addresses: 127.0.0.1, localhost, ::1.
    - Rejects 0.0.0.0, LAN addresses (192.168.x.x, 10.x.x.x), and public IPs.

    Raises ValueError with a clear explanation if host is not permitted.
    Returns normalized host string.
    """
    if not host or not isinstance(host, str):
        raise ValueError("Bind host must be a non-empty string.")

    clean_host = host.strip().lower()
    clean_host_unbracketed = clean_host.strip("[]")

    if strict_mode:
        if clean_host not in ALLOWED_BIND_HOSTS and clean_host_unbracketed not in ALLOWED_BIND_HOSTS:
            raise ValueError(
                f"Bind host '{host}' is prohibited in strict offline mode. "
                f"The server must bind exclusively to loopback ({', '.join(sorted(ALLOWED_BIND_HOSTS))}) "
                "to prevent external network exposure."
            )

    return host.strip()
