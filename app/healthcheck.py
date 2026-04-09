import urllib.request
import urllib.error
import sys

try:
    urllib.request.urlopen('http://localhost:8080/api/auth/me', timeout=3)
    sys.exit(0)
except urllib.error.HTTPError as e:
    # 401 Unauthorized means the app is up but expects auth, which is healthy
    sys.exit(0 if e.code == 401 else 1)
except Exception:
    sys.exit(1)