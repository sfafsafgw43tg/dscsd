"""
Proxy Manager - HTTP/HTTPS/SOCKS5 proxy configuration for emulator
"""
import subprocess
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
            proxy_url: Proxy URL in format:
                - http://[user:pass@]host:port
                - https://[user:pass@]host:port
                - socks5://[user:pass@]host:port
                - host:port (HTTP assumed)
        """
        self.last_error = None
        if not proxy_url or not str(proxy_url).strip():
            return self.clear_proxy()

        try:
            proxy_type, host, port = self._parse_proxy_url(proxy_url)
        except ValueError as exc:
            self.last_error = str(exc)
            print(f"[Proxy] Invalid proxy URL '{proxy_url}': {exc}")
            return False

        if proxy_type in ("http", "https"):
            return self._set_http_proxy(host, port)
        elif proxy_type in ("socks5", "socks4"):
            return self._set_socks_proxy(host, port)
        else:
            self.last_error = f"Unsupported proxy type: {proxy_type}"
            print(f"[Proxy] {self.last_error}")
            return False

    def _parse_proxy_url(self, url):
        """Parse proxy URL into ``(scheme, host, port)``."""
        raw = str(url).strip()
        if not raw:
            raise ValueError("Empty proxy URL")
        if "://" not in raw:
            raw = f"http://{raw}"
        parsed = urlparse(raw)
        proxy_type = (parsed.scheme or "http").lower()
        if proxy_type not in ("http", "https", "socks5", "socks4"):
            raise ValueError(f"Unsupported proxy scheme '{proxy_type}'")
        host = parsed.hostname
        if not host:
            raise ValueError(f"Missing host in proxy URL '{url}'")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"Invalid port in proxy URL '{url}'") from exc
        if port is None:
            port = 8080 if proxy_type in ("http", "https") else 1080
        if not (1 <= port <= 65535):
            raise ValueError(f"Port {port} out of valid range 1-65535")
        return proxy_type, host, int(port)

    def _set_http_proxy(self, host, port):
        """Set HTTP/HTTPS proxy via Android global settings."""
        # Each arg must be a separate list item so Windows adb doesn't mangle it.
        commands = [
            ["shell", "settings", "put", "global", "http_proxy", f"{host}:{port}"],
            ["shell", "settings", "put", "global", "global_http_proxy_host", str(host)],
            ["shell", "settings", "put", "global", "global_http_proxy_port", str(port)],
        ]

        for cmd in commands:
            if self._adb(cmd) is None:
                return False

        print(f"[Proxy] HTTP proxy set: {host}:{port}")
        return True

    def _set_socks_proxy(self, host, port):
        """Configure SOCKS proxy safely (never runs destructive iptables REDIRECT).

        Android's global ``http_proxy`` setting does not natively speak SOCKS5,
        and rewriting ``iptables -t nat OUTPUT`` redirects loopback/ADB traffic
        and drops connectivity. Apply a SOCKS5 proxy at emulator launch via
        ``-http-proxy`` or use an HTTP/HTTPS proxy for live switching.
        """
        self.last_error = (
            f"Live SOCKS5 proxy switching ({host}:{port}) is not natively supported by "
            "Android global settings; save it on the device profile and relaunch, or use "
            "an http://host:port proxy for live switching."
        )
        print(f"[Proxy] {self.last_error}")
        return False

    def clear_proxy(self):
        """Remove proxy settings."""
        commands = [
            ["shell", "settings", "put", "global", "http_proxy", ":0"],
            ["shell", "settings", "delete", "global", "global_http_proxy_host"],
            ["shell", "settings", "delete", "global", "global_http_proxy_port"],
        ]

        for cmd in commands:
            self._adb(cmd)

        print("[Proxy] Proxy cleared")
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

