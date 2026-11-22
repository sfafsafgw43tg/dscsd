"""
Proxy Manager - HTTP/HTTPS/SOCKS5 proxy configuration for emulator
"""
import subprocess
import time
from urllib.parse import urlparse
import config


class ProxyManager:
    """Manage proxy configuration for emulator devices."""

    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
        self.last_error = None

    def _adb(self, command):
        """Execute ADB command."""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                self.last_error = (result.stderr or result.stdout or "").strip() or f"adb exited {result.returncode}"
                return None
            return result.stdout.strip()
        except Exception as e:
            self.last_error = str(e)
            print(f"ADB error: {e}")
            return None

    def set_proxy(self, proxy_url):
        """
        Set proxy for the emulator.

        Args:
            proxy_url: Proxy URL in any accepted format:
                - host:port
                - host:port:login:password
                - login:password@host:port
                - http://[user:pass@]host:port
                - https://[user:pass@]host:port
                - socks5://[user:pass@]host:port
                - socks4://[user:pass@]host:port
                - ++http://host:port  (explicit scheme marker)
                - http://[IPv6]:port, [IPv6]:port:user:pass
        """
        import time as _t
        self.last_error = None
        if not proxy_url or not str(proxy_url).strip():
            return self.clear_proxy()

        try:
            proxy_type, host, port = self._parse_proxy_url(proxy_url)
        except ValueError as exc:
            self.last_error = str(exc)
            print(f"[Proxy] Invalid proxy URL '{proxy_url}': {exc}")
            return False

        # Extract credentials from the URL for live proxy auth
        username, password = self._extract_credentials(proxy_url)

        if proxy_type in ("http", "https"):
            return self._set_http_proxy(host, port, username=username, password=password)
        elif proxy_type in ("socks5", "socks4"):
            return self._set_socks_proxy(host, port, username=username, password=password, scheme=proxy_type)
        else:
            self.last_error = f"Unsupported proxy type: {proxy_type}"
            print(f"[Proxy] {self.last_error}")
            return False

    #: Everything a user may paste from a proxy provider's dashboard. The
    #: aliases all describe a SOCKS5 endpoint; ``socks5h`` only changes who
    #: resolves DNS, which the emulator's own ``-http-proxy`` does not care
    #: about (Android resolves remotely for SOCKS anyway).
    SCHEME_ALIASES = {
        "http": "http",
        "https": "https",     # an HTTP CONNECT proxy; same live method as http
        "socks": "socks5",
        "socks5": "socks5",
        "socks5h": "socks5",
        "socks4": "socks4",
        "socks4a": "socks4",
    }

    def _parse_proxy_url(self, url):
        """Parse a proxy string into ``(scheme, host, port)``.

        Accepts what people actually paste, not just one canonical form:
        ``host:port``, ``host port``, ``http://host:port``,
        ``++http://host:port``, ``socks5://user:pass@host:port``,
        ``host:port:login:password``, ``login:password@host:port``,
        ``socks5h://…``, ``socks://…``, ``socks4a://…``, ``HTTP://…``,
        IPv6 literals (``[::1]:8080`` or ``[2a06:...]:8080:user:pass``) and
        trailing slashes/paths. Raises ``ValueError`` with a message that says
        how to fix it (the dashboard shows that text verbatim).
        """
        raw = str(url or "").strip().strip("'\"")
        if not raw:
            raise ValueError(
                "Empty proxy URL. Enter something like 'host:port', "
                "'http://host:port' or 'socks5://user:pass@host:port'."
            )
        # Strip the ++ prefix that some proxy providers use to denote
        # "use this scheme explicitly" (e.g. ++http://192.168.0.1:8000)
        while raw.startswith("+"):
            raw = raw[1:].lstrip()
        # Paste artefacts people really hit: spaces around the scheme, around
        # the colon, or in place of it ("1.2.3.4 : 8080", "host 8080").
        raw = raw.replace(" : ", ":").replace(": ", ":").replace(" :", ":")
        if "://" in raw:
            scheme_part, _, rest = raw.partition("://")
            raw = f"{scheme_part.strip()}://{rest.strip()}"
        # "host port" (a space instead of a colon) is a common paste artefact.
        if "://" not in raw and raw.count(":") == 0 and " " in raw:
            head, _, tail = raw.rpartition(" ")
            if tail.strip().isdigit():
                raw = f"{head.strip()}:{tail.strip()}"
        if "://" not in raw:
            raw = f"http://{raw}"
        scheme_part, _, rest = raw.partition("://")
        rest = self._rewrite_host_port_user_pass(rest)
        raw = f"{scheme_part}://{rest}"
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "http").lower()
        proxy_type = self.SCHEME_ALIASES.get(scheme)
        if proxy_type is None:
            raise ValueError(
                f"Unsupported proxy scheme '{scheme}'. Use http://, https://, "
                "socks5:// or socks4:// (a bare 'host:port' means HTTP)."
            )
        host = parsed.hostname
        if not host:
            raise ValueError(
                f"Missing host in proxy URL '{url}'. Expected "
                "'scheme://[user:pass@]host:port'."
            )
        if " " in host:
            raise ValueError(
                f"'{host}' is not a valid host. Expected "
                "'scheme://[user:pass@]host:port' (no spaces)."
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"Invalid port in proxy URL '{url}'") from exc
        if port is None:
            port = 8080 if proxy_type in ("http", "https") else 1080
        if not (1 <= port <= 65535):
            raise ValueError(f"Port {port} out of valid range 1-65535")
        return proxy_type, host, int(port)

    @staticmethod
    def _rewrite_host_port_user_pass(rest):
        """Convert ``host:port:user:pass`` or ``[ipv6]:port:user:pass`` to ``user:pass@host:port``."""
        head, sep, tail = rest.partition("/")
        if "@" in head:
            return rest
        if head.startswith("["):
            bracket_end = head.find("]")
            if bracket_end == -1:
                return rest
            host_part = head[:bracket_end + 1]
            after = head[bracket_end + 1:]
            if not after.startswith(":"):
                return rest
            parts = after[1:].split(":", 2)
            if len(parts) == 3 and parts[0].isdigit():
                port_s, user, passwd = parts
                return f"{user}:{passwd}@{host_part}:{port_s}{sep}{tail}"
            return rest
        parts = head.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            host_part, port_s, user, passwd = parts
            return f"{user}:{passwd}@{host_part}:{port_s}{sep}{tail}"
        return rest

    def check_proxy_ip(self, url, timeout=8):
        """Query an external IP-echo endpoint through *url* and return ``{"ok", "ip", "error"}``."""
        raw = str(url or "").strip()
        if not raw:
            return {"ok": False, "ip": None, "error": "No proxy URL"}
        try:
            report = self.parse_report(raw)
            if not report.get("valid"):
                return {"ok": False, "ip": None, "error": report.get("error") or "Invalid proxy"}
            proxy_url = report["normalised"]
            import requests
            resp = requests.get(
                "https://api.ipify.org?format=json",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=timeout,
            )
            resp.raise_for_status()
            ip = (resp.json() or {}).get("ip")
            if ip:
                return {"ok": True, "ip": str(ip).strip(), "error": None}
            return {"ok": False, "ip": None, "error": "Empty IP response"}
        except Exception as exc:
            return {"ok": False, "ip": None, "error": str(exc)}

    def parse_report(self, url):
        """``{"valid", "scheme", "type", "host", "port", "normalised", "error"}``.

        The dashboard validates as you type, so it needs the parsed pieces and
        a message to show - not a raised exception.
        """
        raw = str(url or "").strip()
        if not raw:
            return {"valid": False, "error": "", "empty": True}
        try:
            proxy_type, host, port = self._parse_proxy_url(raw)
        except ValueError as exc:
            return {"valid": False, "error": str(exc)}
        userinfo = ""
        bare = raw.replace(" : ", ":").split("://", 1)[-1]
        bare = self._rewrite_host_port_user_pass(bare)
        if "@" in bare.split("/")[0]:
            userinfo = bare.rsplit("@", 1)[0] + "@"
        scheme = (raw.split("://", 1)[0].strip().lower()
                  if "://" in raw else proxy_type)
        host_fmt = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return {
            "valid": True,
            "scheme": scheme,
            "type": proxy_type,
            "host": host,
            "port": port,
            "has_credentials": bool(userinfo),
            "normalised": f"{proxy_type}://{userinfo}{host_fmt}:{port}",
            "error": None,
        }

    def _extract_credentials(self, proxy_url):
        """Extract (username, password) from a proxy URL if present."""
        bare = str(proxy_url).split("://", 1)[-1] if "://" in str(proxy_url) else str(proxy_url)
        bare = self._rewrite_host_port_user_pass(bare)
        if "@" in bare.split("/")[0]:
            userinfo = bare.rsplit("@", 1)[0]
            if ":" in userinfo:
                user, pwd = userinfo.split(":", 1)
                return user, pwd
            return userinfo, ""
        return None, None

    def _console_port(self):
        """Extract the emulator console port from the device serial."""
        device = str(self.device_serial or "")
        if device.startswith("emulator-"):
            try:
                return int(device.rsplit("-", 1)[-1])
            except (TypeError, ValueError):
                pass
        return None

    def _apply_console_proxy(self, proxy_uri):
        """Set proxy via emulator console (QEMU/slirp level — routes ALL TCP).

        This is the ONLY method that routes native-code and app traffic
        through a SOCKS proxy. Android settings do not support SOCKS.
        """
        from core import emulator_console
        port = self._console_port()
        if not port:
            self.last_error = f"No console port for serial {self.device_serial}"
            return False
        try:
            ok = emulator_console.send_commands(port, [f"http-proxy {proxy_uri}"])
            if not ok:
                print(f"[Proxy] Console http-proxy error: {emulator_console.last_error}")
            return ok
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _set_http_proxy(self, host, port, username=None, password=None):
        """Set HTTP/HTTPS proxy live (Android settings + emulator console).

        HTTP proxies are typically also passed via -http-proxy at launch
        (QEMU level), which routes ALL TCP including native code. This live
        setter is for post-boot Apply clicks and keeps Java and QEMU in sync.
        """
        if username:
            user_pwd = f"{username}:{password}@" if password else f"{username}@"
        else:
            user_pwd = ""
        proxy_uri = f"http://{user_pwd}{host}:{port}"
        proxy_string = f"{user_pwd}{host}:{port}"

        # --- Android settings (Java-side ProxySelector / WebView / apps) ---
        settings_cmds = [
            ["shell", "settings", "put", "global", "http_proxy", proxy_string],
            ["shell", "settings", "put", "global", "global_http_proxy_host", host],
            ["shell", "settings", "put", "global", "global_http_proxy_port", str(port)],
        ]
        if username:
            settings_cmds.append(["shell", "settings", "put", "global", "global_http_proxy_username", username])
            if password:
                settings_cmds.append(["shell", "settings", "put", "global", "global_http_proxy_password", password])
        else:
            settings_cmds.append(["shell", "settings", "delete", "global", "global_http_proxy_username"])
            settings_cmds.append(["shell", "settings", "delete", "global", "global_http_proxy_password"])
        settings_cmds.append(["shell", "settings", "put", "global", "global_http_proxy_exclusion_list",
                              "localhost,127.0.0.1"])

        for cmd in settings_cmds:
            self._adb(cmd)

        # --- Emulator console (QEMU/slirp level - routes native code too) ---
        self._apply_console_proxy(proxy_uri)

        # Cycle Wi-Fi so ConnectivityManager reloads
        self._adb(["shell", "svc", "wifi", "disable"])
        time.sleep(0.5)
        self._adb(["shell", "svc", "wifi", "enable"])

        print(f"[Proxy] HTTP proxy set live on {self.device_serial}: {host}:{port}")
        self.last_error = None
        return True

    def _set_socks_proxy(self, host, port, username=None, password=None, scheme="socks5"):
        """Set SOCKS4/SOCKS5 proxy live via emulator console (QEMU level).

        CRITICAL: Android's settings provider does NOT support SOCKS proxies.
        Setting global_http_proxy_host/port to a SOCKS endpoint makes Android
        try HTTP CONNECT to it and all connections fail. SOCKS only works
        when applied at the QEMU slirp level via the emulator console
        'http-proxy' command — that routes ALL guest TCP through SOCKS,
        including native code, DNS (in SOCKS5 remote-DNS mode), and Java.

        We also write a marker to Android settings so the Settings UI shows
        a proxy is configured (avoids user confusion), but we do NOT set
        global_http_proxy_host/port because those trigger Android's HTTP
        proxy stack which doesn't speak SOCKS.
        """
        if username:
            user_pwd = f"{username}:{password}@" if password else f"{username}@"
        else:
            user_pwd = ""
        proxy_uri = f"{scheme}://{user_pwd}{host}:{port}"

        # Clear any stale HTTP proxy settings (they would conflict)
        for cmd in (
            ["shell", "settings", "delete", "global", "global_http_proxy_host"],
            ["shell", "settings", "delete", "global", "global_http_proxy_port"],
            ["shell", "settings", "delete", "global", "global_http_proxy_username"],
            ["shell", "settings", "delete", "global", "global_http_proxy_password"],
        ):
            self._adb(cmd)

        # Write a human-readable marker so apps/UI can see a proxy is set
        self._adb(["shell", "settings", "put", "global", "http_proxy", proxy_uri])

        # --- Emulator console (QEMU/slirp level) ---
        # This is the ONLY mechanism that actually routes traffic through SOCKS.
        console_ok = self._apply_console_proxy(proxy_uri)
        if not console_ok:
            # Retry without auth in case the console doesn't accept SOCKS
            # auth in this emulator build; some versions need host:port only
            # and auth is handled by the proxy server requiring it upstream.
            console_ok = self._apply_console_proxy(f"{scheme}://{host}:{port}")

        # Cycle Wi-Fi so connections use the new proxy
        self._adb(["shell", "svc", "wifi", "disable"])
        time.sleep(0.5)
        self._adb(["shell", "svc", "wifi", "enable"])

        device = str(self.device_serial or "")
        if console_ok:
            print(f"[Proxy] {scheme.upper()} proxy set live on {device}: {host}:{port} (QEMU console)")
        else:
            print(f"[Proxy] {scheme.upper()} proxy setting saved on {device}: {host}:{port} "
                  f"(console: {self.last_error})")
        self.last_error = None
        return True

    def clear_proxy(self):
        """Remove proxy settings from Android and emulator console."""
        # Clear Android settings
        for cmd in (
            ["shell", "settings", "put", "global", "http_proxy", ":0"],
            ["shell", "settings", "delete", "global", "global_http_proxy_host"],
            ["shell", "settings", "delete", "global", "global_http_proxy_port"],
            ["shell", "settings", "delete", "global", "global_http_proxy_username"],
            ["shell", "settings", "delete", "global", "global_http_proxy_password"],
            ["shell", "settings", "delete", "global", "global_http_proxy_exclusion_list"],
        ):
            self._adb(cmd)

        # Clear console proxy (issuing 'http-proxy' with no argument resets it
        # to direct connections in the Android emulator console).
        from core import emulator_console
        port = self._console_port()
        if port:
            try:
                emulator_console.send_commands(port, ["http-proxy"])
            except Exception:
                pass

        print(f"[Proxy] Proxy cleared on {self.device_serial}")
        return True

    def get_current_proxy(self):
        """Get current proxy settings."""
        result = self._adb(["shell", "settings", "get", "global", "http_proxy"])
        return result if result and result not in (":0", "null", "None") else None

    def test_proxy(self, test_url="http://ip-api.com/json", proxy_url=None):
        """Test connectivity via host urllib (when proxy_url given) or device tool."""
        import json
        if proxy_url:
            try:
                import urllib.request
                proxy_type, host, port = self._parse_proxy_url(proxy_url)
                if proxy_type in ("http", "https"):
                    handler = urllib.request.ProxyHandler({
                        "http": proxy_url if "://" in proxy_url else f"http://{host}:{port}",
                        "https": proxy_url if "://" in proxy_url else f"http://{host}:{port}",
                    })
                    opener = urllib.request.build_opener(handler)
                    with opener.open(test_url, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))
                        return {
                            "ip": data.get("query"),
                            "country": data.get("country"),
                            "isp": data.get("isp"),
                        }
            except Exception as exc:
                self.last_error = str(exc)
                return None

        # On-device check (try curl, then toybox/busybox wget)
        for cmd in (
            ["shell", "curl", "-s", "--max-time", "8", test_url],
            ["shell", "wget", "-qO-", "-T", "8", test_url],
        ):
            result = self._adb(cmd)
            if result:
                try:
                    data = json.loads(result)
                    return {
                        "ip": data.get("query"),
                        "country": data.get("country"),
                        "isp": data.get("isp"),
                    }
                except (ValueError, TypeError):
                    pass
        return None


class ProxyRotator:
    """Rotate through multiple proxies"""
    
    def __init__(self, proxies=None):
        self.proxies = proxies or []
        self.current_index = 0
    
    def add_proxy(self, proxy_url):
        """Add proxy to rotation list"""
        self.proxies.append(proxy_url)
    
    def next(self):
        """Get next proxy in rotation"""
        if not self.proxies:
            return None
        
        proxy = self.proxies[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxies)
        return proxy
    
    def load_from_file(self, filepath):
        """Load proxies from file (one per line)"""
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self.proxies.append(line)
            return len(self.proxies)
        except Exception as e:
            print(f"Failed to load proxies: {e}")
            return 0


class ProxyQualityChecker:
    """Check proxy quality for anti-detection"""
    
    # Known datacenter ASN prefixes (partial list)
    DATACENTER_ASNS = [
        "AS14061",  # DigitalOcean
        "AS16509",  # Amazon AWS
        "AS15169",  # Google
        "AS8075",   # Microsoft
        "AS13335",  # Cloudflare
        "AS20940",  # Akamai
        "AS24940",  # Hetzner
        "AS14618",  # Amazon
        "AS16276",  # OVH
        "AS51167",  # Contabo
    ]
    
    def __init__(self, device_serial="emulator-5554"):
        self.device_serial = device_serial
        self.adb = config.ADB_PATH
    
    def _adb(self, command):
        """Execute ADB command"""
        cmd = [self.adb, "-s", self.device_serial] + command
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        except Exception as e:
            print(f"ADB error: {e}")
            return None
    
    def get_ip_info(self, use_adb=True):
        """Get current external IP information"""
        test_url = "http://ip-api.com/json?fields=status,message,country,countryCode,region,regionName,city,zip,lat,lon,timezone,isp,org,as,mobile,proxy,hosting,query"
        
        if use_adb:
            result = self._adb(["shell", "curl", "-s", test_url])
        else:
            try:
                import urllib.request
                with urllib.request.urlopen(test_url, timeout=10) as response:
                    result = response.read().decode()
            except Exception:
                return None
        
        if result:
            try:
                import json
                return json.loads(result)
            except Exception:
                pass
        return None
    
    def check_ip_reputation(self, use_adb=True):
        """Check if IP is flagged or from datacenter"""
        info = self.get_ip_info(use_adb)
        if not info:
            return {"error": "Could not get IP info"}
        
        reputation = {
            "ip": info.get("query"),
            "country": info.get("country"),
            "city": info.get("city"),
            "isp": info.get("isp"),
            "org": info.get("org"),
            "asn": info.get("as", ""),
            "timezone": info.get("timezone"),
            "is_mobile": info.get("mobile", False),
            "is_proxy": info.get("proxy", False),
            "is_hosting": info.get("hosting", False),
            "is_datacenter": False,
            "quality_score": 100,
            "warnings": []
        }
        
        # Check for datacenter IP
        asn = info.get("as", "")
        for dc_asn in self.DATACENTER_ASNS:
            if dc_asn in asn:
                reputation["is_datacenter"] = True
                reputation["quality_score"] -= 40
                reputation["warnings"].append(f"Datacenter IP detected ({dc_asn})")
                break
        
        # Check for hosting/proxy flags
        if info.get("hosting"):
            reputation["quality_score"] -= 30
            reputation["warnings"].append("Marked as hosting provider")
        
        if info.get("proxy"):
            reputation["quality_score"] -= 20
            reputation["warnings"].append("Marked as proxy")
        
        # Mobile IP is good for Android
        if info.get("mobile"):
            reputation["quality_score"] += 10
        
        return reputation
    
    def is_residential(self, use_adb=True):
        """Check if current IP appears to be residential"""
        rep = self.check_ip_reputation(use_adb)
        
        if "error" in rep:
            return False
        
        # Residential = not datacenter, not hosting, not flagged as proxy
        return (
            not rep["is_datacenter"] and 
            not rep["is_hosting"] and 
            not rep["is_proxy"]
        )
    
    def get_recommended_timezone(self, use_adb=True):
        """Get timezone that matches current IP location"""
        info = self.get_ip_info(use_adb)
        if info:
            return info.get("timezone")
        return None
    
    def match_timezone_to_ip(self):
        """Set device timezone to match IP location"""
        timezone = self.get_recommended_timezone()
        if timezone:
            self._adb(["shell", "settings", "put", "global", "auto_time_zone", "0"])
            self._adb(["shell", "setprop", "persist.sys.timezone", timezone])
            return timezone
        return None
    
    def check_dns_leak(self):
        """Check for DNS leaks that could reveal true location"""
        # Use DNS leak test service
        dns_test_domains = [
            "whoami.akamai.net",
        ]
        
        dns_servers = []
        for domain in dns_test_domains:
            result = self._adb(["shell", "nslookup", domain])
            if result:
                # Extract DNS server from response
                for line in result.split("\n"):
                    if "Server:" in line:
                        server = line.split(":")[-1].strip()
                        if server:
                            dns_servers.append(server)
        
        return {
            "dns_servers": dns_servers,
            "potential_leak": len(set(dns_servers)) > 1
        }
    
    def set_dns_servers(self, dns_servers=None):
        """Set custom DNS servers to prevent leaks"""
        if dns_servers is None:
            # Use privacy-friendly DNS
            dns_servers = ["1.1.1.1", "1.0.0.1"]  # Cloudflare
        
        # Set on device (requires root)
        for i, dns in enumerate(dns_servers[:2], 1):
            self._adb(["shell", "setprop", f"net.dns{i}", dns])
        
        return dns_servers
    
    def validate_proxy_quality(self, min_score=60):
        """Full proxy quality validation"""
        print("[ProxyQuality] Checking proxy quality...")
        
        reputation = self.check_ip_reputation()
        if "error" in reputation:
            return False, reputation
        
        print(f"  IP: {reputation['ip']}")
        print(f"  Location: {reputation['city']}, {reputation['country']}")
        print(f"  ISP: {reputation['isp']}")
        print(f"  Quality Score: {reputation['quality_score']}/100")
        
        if reputation['warnings']:
            print(f"  Warnings: {', '.join(reputation['warnings'])}")
        
        passed = reputation['quality_score'] >= min_score
        
        if not passed:
            print(f"  [FAIL] Quality score below minimum ({min_score})")
        else:
            print(f"  [PASS] Proxy quality acceptable")
        
        return passed, reputation


def validate_and_setup_proxy(device_serial, proxy_url=None, auto_timezone=True):
    """Convenience function to validate proxy and configure device"""
    manager = ProxyManager(device_serial)
    checker = ProxyQualityChecker(device_serial)
    
    if proxy_url:
        manager.set_proxy(proxy_url)
    
    # Check quality
    passed, reputation = checker.validate_proxy_quality()
    
    if passed and auto_timezone:
        # Match timezone to IP
        tz = checker.match_timezone_to_ip()
        if tz:
            print(f"  Timezone set to: {tz}")
    
    return passed, reputation

