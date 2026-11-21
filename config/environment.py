import ipaddress
import re
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured


def environment_settings(env, base_dir):
    def text(name, default=""):
        return env.get(name, default).strip()

    def boolean(name, default):
        value = text(name, str(default)).lower()
        if value not in {"true", "false", "1", "0"}:
            raise ImproperlyConfigured(f"{name} must be true or false.")
        return value in {"true", "1"}

    def integer(name, default, minimum=1, maximum=600000):
        try:
            value = int(text(name, str(default)))
        except ValueError:
            raise ImproperlyConfigured(f"{name} must be an integer.") from None
        if not minimum <= value <= maximum:
            raise ImproperlyConfigured(f"{name} must be between {minimum} and {maximum}.")
        return value

    profile = text("APP_ENV", "development").lower()
    if profile not in {"development", "test", "production"}:
        raise ImproperlyConfigured("APP_ENV must be development, test or production.")
    production = profile == "production"
    debug = boolean("DEBUG", not production)
    secret = env.get("SECRET_KEY", "" if production else "dev-insecure-key-change-in-production")
    hosts = [value.strip() for value in text(
        "ALLOWED_HOSTS", "" if production else "localhost,127.0.0.1",
    ).split(",") if value.strip()]
    if production:
        if debug:
            raise ImproperlyConfigured("DEBUG must be false in production.")
        if (len(secret) < 50 or len(set(secret)) < 5 or secret.startswith("django-insecure-")
                or any(word in secret.lower() for word in ("replace", "change-me", "placeholder"))):
            raise ImproperlyConfigured("SECRET_KEY must be a strong production secret.")
        if not hosts or not all(_valid_host(host) for host in hosts):
            raise ImproperlyConfigured("ALLOWED_HOSTS must contain explicit hostnames or IP addresses.")

    engine = text("DB_ENGINE", "sqlite").lower()
    if engine not in {"sqlite", "postgresql"} or (production and engine != "postgresql"):
        raise ImproperlyConfigured("DB_ENGINE must be sqlite or postgresql; production requires postgresql.")
    if engine == "sqlite":
        database = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": text("DB_NAME") or base_dir / "db.sqlite3",
            "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
        }
    else:
        if production:
            for name in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST"):
                if not text(name):
                    raise ImproperlyConfigured(f"{name} is required in production.")
            if len(env["DB_PASSWORD"]) < 16:
                raise ImproperlyConfigured("DB_PASSWORD must contain at least 16 characters in production.")
        sslmode = text("DB_SSLMODE", "verify-full" if production else "prefer")
        if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ImproperlyConfigured("DB_SSLMODE is invalid.")
        if production and sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ImproperlyConfigured("DB_SSLMODE must require TLS in production.")
        statement_timeout = integer("DB_STATEMENT_TIMEOUT_MS", 15000)
        lock_timeout = integer("DB_LOCK_TIMEOUT_MS", 5000)
        database = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": text("DB_NAME", "campus_lost_found"),
            "USER": text("DB_USER", "campus_app"),
            "PASSWORD": env.get("DB_PASSWORD", ""),
            "HOST": text("DB_HOST", "127.0.0.1"),
            "PORT": integer("DB_PORT", 5432, maximum=65535),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": integer("DB_CONNECT_TIMEOUT", 5, maximum=60),
                "sslmode": sslmode,
                "options": f"-c statement_timeout={statement_timeout} -c lock_timeout={lock_timeout}",
            },
        }

    redis_url = text("REQUEST_LIMITS_REDIS_URL")
    if production and not redis_url:
        raise ImproperlyConfigured("REQUEST_LIMITS_REDIS_URL is required in production.")
    if redis_url:
        try:
            parsed = urlsplit(redis_url)
            valid = (parsed.scheme in {"redis", "rediss"} and parsed.hostname
                     and not parsed.query and not parsed.fragment
                     and not any(character.isspace() for character in redis_url)
                     and "," not in redis_url and parsed.port != 0
                     and (not parsed.path or re.fullmatch(r"/\d*", parsed.path)))
        except ValueError:
            valid = False
        if not valid:
            raise ImproperlyConfigured("REQUEST_LIMITS_REDIS_URL must identify one Redis primary.")
    prefix = text("REQUEST_LIMITS_KEY_PREFIX", "campus-lost-found")
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", prefix):
        raise ImproperlyConfigured("REQUEST_LIMITS_KEY_PREFIX must be 1 to 100 letters, digits, :, _ or -.")
    proxies = [value.strip() for value in text("REQUEST_LIMITS_TRUSTED_PROXIES").split(",")
               if value.strip()]
    try:
        for proxy in proxies:
            ipaddress.ip_network(proxy)
    except ValueError:
        raise ImproperlyConfigured("REQUEST_LIMITS_TRUSTED_PROXIES must contain valid IP networks.") from None
    trust_https = boolean("TRUST_PROXY_HTTPS", False)
    if trust_https and not proxies:
        raise ImproperlyConfigured("TRUST_PROXY_HTTPS requires REQUEST_LIMITS_TRUSTED_PROXIES.")
    origins = [value.strip() for value in text("CSRF_TRUSTED_ORIGINS").split(",") if value.strip()]
    if production and any(not _valid_origin(origin) for origin in origins):
        raise ImproperlyConfigured("CSRF_TRUSTED_ORIGINS must contain explicit HTTPS origins.")
    hsts = integer("SECURE_HSTS_SECONDS", 3600 if production else 0, minimum=0, maximum=63072000)
    subdomains = boolean("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
    preload = boolean("SECURE_HSTS_PRELOAD", False)
    if preload and (not subdomains or hsts < 31536000):
        raise ImproperlyConfigured("SECURE_HSTS_PRELOAD requires subdomains and at least 31536000 seconds.")
    return {
        "APP_ENV": profile, "DEBUG": debug, "SECRET_KEY": secret, "ALLOWED_HOSTS": hosts,
        "DATABASES": {"default": database}, "CSRF_TRUSTED_ORIGINS": origins,
        "REQUEST_LIMITS_REDIS_URL": redis_url, "REQUEST_LIMITS_KEY_PREFIX": prefix,
        "REQUEST_LIMITS_TRUSTED_PROXIES": proxies,
        "SECURE_PROXY_SSL_HEADER": ("HTTP_X_FORWARDED_PROTO", "https") if trust_https else None,
        "SECURE_SSL_REDIRECT": production, "SESSION_COOKIE_SECURE": production,
        "CSRF_COOKIE_SECURE": production, "SECURE_HSTS_SECONDS": hsts,
        "SECURE_HSTS_INCLUDE_SUBDOMAINS": subdomains, "SECURE_HSTS_PRELOAD": preload,
    }


def _valid_host(host):
    try:
        if host.startswith("[") and host.endswith("]"):
            ipaddress.IPv6Address(host[1:-1])
        else:
            ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return len(host) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.split(".")
        )


def _valid_origin(origin):
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and _valid_host(f"[{host}]" if ":" in host else host)
                and not parsed.username and not parsed.password and not parsed.path
                and not parsed.query and not parsed.fragment and parsed.port != 0)
    except ValueError:
        return False
