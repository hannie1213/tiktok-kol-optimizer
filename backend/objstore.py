"""
Object storage for files that must outlive a redeploy (AI video clips).

Any S3-compatible bucket works; Cloudflare R2 is the cheap default (10 GB
free, no egress fees). Hosts such as Render wipe the container's own disk on
every deploy, so without a bucket the clips only live until the next one.

Config (env) — all four are needed to switch it on:
  S3_ENDPOINT            e.g. https://<account-id>.r2.cloudflarestorage.com
  S3_BUCKET              bucket name, e.g. lumio-videos
  S3_ACCESS_KEY_ID       R2: "Manage R2 API tokens" → access key id
  S3_SECRET_ACCESS_KEY   … and its secret
  S3_REGION              optional, default "auto" (R2); AWS needs the real region

Requests are signed with AWS Signature Version 4 using only the standard
library, so there is no extra dependency. Objects stay private; downloads go
through short-lived pre-signed URLs.
"""
import datetime as _dt
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = (os.environ.get("S3_ENDPOINT") or "").strip().rstrip("/")
BUCKET = (os.environ.get("S3_BUCKET") or "").strip()
ACCESS_KEY = (os.environ.get("S3_ACCESS_KEY_ID") or "").strip()
SECRET_KEY = (os.environ.get("S3_SECRET_ACCESS_KEY") or "").strip()
REGION = (os.environ.get("S3_REGION") or "auto").strip()
_SERVICE = "s3"


def enabled() -> bool:
    return bool(ENDPOINT and BUCKET and ACCESS_KEY and SECRET_KEY)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(date: str) -> bytes:
    k = _sign(("AWS4" + SECRET_KEY).encode("utf-8"), date)
    k = _sign(k, REGION)
    k = _sign(k, _SERVICE)
    return _sign(k, "aws4_request")


def _target(key: str):
    parsed = urllib.parse.urlparse(ENDPOINT)
    path = "/" + urllib.parse.quote(BUCKET) + "/" + urllib.parse.quote(key.lstrip("/"), safe="/-_.~")
    return parsed.scheme or "https", parsed.netloc, path


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _signed_headers(method: str, key: str, payload_hash: str, extra: dict = None):
    scheme, host, path = _target(key)
    t = _now()
    amz_date, date = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    headers.update({k.lower(): v for k, v in (extra or {}).items()})
    names = sorted(headers)
    canonical = "\n".join([method, path, "", "".join(f"{n}:{headers[n].strip()}\n" for n in names),
                           ";".join(names), payload_hash])
    scope = f"{date}/{REGION}/{_SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(date), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={sig}")
    return f"{scheme}://{host}{path}", headers


def _send(method: str, key: str, data: bytes = None, content_type: str = None, timeout: int = 120):
    payload_hash = hashlib.sha256(data or b"").hexdigest()
    extra = {"content-type": content_type} if content_type else None
    url, headers = _signed_headers(method, key, payload_hash, extra)
    headers.pop("host", None)                       # urllib sets it from the URL
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def put_file(key: str, path: str, content_type: str = "application/octet-stream") -> None:
    with open(path, "rb") as f:
        data = f.read()
    _send("PUT", key, data, content_type)


def delete(key: str) -> None:
    try:
        _send("DELETE", key, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def presigned_get(key: str, expires: int = 600, filename: str = None) -> str:
    """A URL that downloads the object for `expires` seconds, no credentials needed."""
    scheme, host, path = _target(key)
    t = _now()
    amz_date, date = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    scope = f"{date}/{REGION}/{_SERVICE}/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{ACCESS_KEY}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    if filename:
        params["response-content-disposition"] = f'attachment; filename="{filename}"'
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                     for k, v in sorted(params.items()))
    canonical = "\n".join(["GET", path, query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(date), to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{path}?{query}&X-Amz-Signature={sig}"
