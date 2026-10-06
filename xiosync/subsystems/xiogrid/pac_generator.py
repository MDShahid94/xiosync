"""PAC file generator for per-domain proxy routing (Hierarchical Network Layer 3).

Chrome supports --proxy-pac-url=file:///path/to/proxy.pac which defines per-URL
routing rules. This module generates PAC files dynamically based on:
- Profile-level default proxy (Layer 2)
- Domain-specific overrides (Layer 3)
- Workflow override (Layer 4 — replaces everything)

The PAC file is critical because it allows domain-level routing WITHIN a single
Chrome instance, preserving the shared cookie jar. Using separate Playwright
contexts per domain would isolate cookies, breaking OAuth relay (e.g. v0 needs
Google's cookies).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DomainProxyRule:
    domain_pattern: str
    proxy_url: str
    priority: int = 0


@dataclass(frozen=True)
class PACConfig:
    profile_default_proxy: str
    domain_rules: list[DomainProxyRule] = field(default_factory=list)
    workflow_override: str | None = None


def _socks_url_to_pac_proxy(socks_url: str) -> str:
    """Convert 'socks5://host:port' to 'SOCKS5 host:port' for PAC syntax."""
    prefix = ""
    if socks_url.startswith("socks5://"):
        prefix = "socks5://"
    elif socks_url.startswith("socks5h://"):
        prefix = "socks5h://"
    
    if prefix:
        return f"SOCKS5 {socks_url[len(prefix):]}"
    return socks_url


def generate_pac_file(config: PACConfig) -> str:
    """Generate a valid JavaScript PAC file string."""
    if config.workflow_override:
        proxy = _socks_url_to_pac_proxy(config.workflow_override)
        return f"function FindProxyForURL(url, host) {{\n    return '{proxy}';\n}}"

    lines = ["function FindProxyForURL(url, host) {"]
    
    # Sort rules by priority descending
    sorted_rules = sorted(config.domain_rules, key=lambda r: r.priority, reverse=True)
    
    for rule in sorted_rules:
        proxy = _socks_url_to_pac_proxy(rule.proxy_url)
        domain = rule.domain_pattern
        # Use dnsDomainIs for domain matching
        if domain.startswith('.'):
            # dnsDomainIs(host, ".example.com")
            lines.append(f"    if (dnsDomainIs(host, '{domain}')) {{\n        return '{proxy}';\n    }}")
        else:
            # Need to match exact domain or subdomains if domain doesn't start with '.'
            # dnsDomainIs(host, "example.com") is true for www.example.com
            lines.append(f"    if (host === '{domain}' || dnsDomainIs(host, '.{domain}')) {{\n        return '{proxy}';\n    }}")
            
    default_proxy = _socks_url_to_pac_proxy(config.profile_default_proxy)
    lines.append(f"    return '{default_proxy}';\n}}")
    
    return "\n".join(lines)


def write_pac_for_session(session_id: str, config: PACConfig, directory: str = '/tmp') -> str:
    """Write PAC file to disk and return file:// URL for Chrome --proxy-pac-url."""
    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, f"xio_pac_{session_id}.pac")
    pac_content = generate_pac_file(config)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(pac_content)
    return f"file://{file_path}"


def cleanup_pac_file(session_id: str, directory: str = '/tmp') -> bool:
    """Delete the PAC file for a session. Returns True if file was deleted."""
    file_path = os.path.join(directory, f"xio_pac_{session_id}.pac")
    if os.path.exists(file_path):
        os.remove(file_path)
        return True
    return False


def build_chrome_proxy_args(proxy_url: str | None = None, pac_url: str | None = None) -> list[str]:
    """Return the Chrome CLI arguments for proxy configuration."""
    if pac_url:
        return [f'--proxy-pac-url={pac_url}']
    if proxy_url:
        return [f'--proxy-server={proxy_url}']
    return []
