"""网络容错 — import 即生效，自动给 requests 加 SSL/超时/连接 重试

v6.1.19:加 curl_cffi TLS retry helper — 解 curl_cffi/BoringSSL 客戶端 library bug
        (error:00000000:invalid library / OPENSSL_internal),
        GitHub upstream 未修(curl_cffi#601, yfinance#2633, yt-dlp#15385)
"""
import random
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_MAX_RETRIES = 3
_RETRY_DELAY = 2

_orig_get = requests.get
_orig_post = requests.post
_orig_put = requests.put

_RETRY_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectTimeout,
)

# v6.1.20.6: 拔掉 v6.1.20.5 加的全域 _REQUESTS_LOCK。
# Why: 慢的 stdlib requests (SYB stoken 後台 thread 跑 verify_stoken,timeout 30s × 3 retry = ~96s)
#      持 lock 太久,主線程 _build_ui / 其它 tab 初始化要 requests 時 deadlock,
#      導致 deiconify() 永遠不到 → 窗口永遠藏著(用戶看到「卡住」)。
# 而且 Python 3.12.3 stdlib ssl bug 是單 thread 也會 crash,序列化 lock 救不了根本問題。
# 真凶 dump_traceback_later 已在 app.py 拔掉,這個 lock 屬於誤入歧途,撤回。


def _wrap(orig):
    def wrapper(*a, **kw):
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                return orig(*a, **kw)
            except _RETRY_ERRORS as e:
                last_err = e
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAY)
        raise last_err
    return wrapper


requests.get = _wrap(_orig_get)
requests.post = _wrap(_orig_post)
requests.put = _wrap(_orig_put)


# ── curl_cffi 網路問題 retry helper(v6.1.19,v6.1.20.2 擴大 VPN 場景)──
# 偵測「應該 retry」的暫時性錯誤:
#  - curl_cffi BoringSSL library bug(error 35 invalid library)
#  - VPN / 跨境網路常見錯誤:timeout / connection reset / connection refused
_CURL_CFFI_TLS_KEYWORDS = (
    "TLS connect error",
    "invalid library",
    "OPENSSL_internal",
    "SSL_ERROR_SYSCALL",
    "Failed to perform",
    # v6.1.20.2:VPN / 中國跨境網路常見錯誤,也該 retry
    "timed out",                 # curl 28 (CURLE_OPERATION_TIMEDOUT)
    "Operation timed out",
    "Connection timed out",
    "Connection reset",          # curl 56 (CURLE_RECV_ERROR)
    "Connection refused",        # curl 7  (CURLE_COULDNT_CONNECT)
    "Could not resolve",         # curl 6  (CURLE_COULDNT_RESOLVE_HOST)— DNS
    "Empty reply",               # curl 52 (CURLE_GOT_NOTHING)
    "Recv failure",              # curl 56 變體
    "Send failure",              # curl 55
    "OpenSSL SSL_read",          # SSL 中斷
    "connection was forcibly",   # Windows TCP RST
)


def is_curl_cffi_tls_error(exc: BaseException) -> bool:
    """判斷異常是否是 curl_cffi 暫時性錯誤(可 retry)。

    包含:
      - BoringSSL library bug (error:00000000:invalid library)
      - 連線超時 / DNS / 連線中斷 (VPN 場景常見)

    範例 match:
      'Failed to perform, curl: (28) Connection timed out after 20004 milliseconds...'
      'Failed to perform, curl: (35) TLS connect error: ...invalid library...'
      'Failed to perform, curl: (56) OpenSSL SSL_read: Connection was reset...'
    """
    s = str(exc) if exc else ""
    return any(kw in s for kw in _CURL_CFFI_TLS_KEYWORDS)


def cffi_retry_call(
    func,
    *args,
    max_retries: int = 2,
    base_backoff: float = 0.8,
    on_retry=None,
    long_backoff_for_timeout: bool = True,
    **kwargs,
):
    """執行 curl_cffi 請求,遇 TLS library bug / 連線超時 / DNS 失敗自動 retry。

    Args:
        func: 要呼叫的函式(e.g. session.get / session.post)
        max_retries: 最多 retry 次數(額外嘗試),total = max_retries + 1
        base_backoff: 初次 retry 等候(秒)。指數退避 base * 2^attempt + jitter
        on_retry: callback(attempt:int, exc:Exception) — 通常餵 log
        long_backoff_for_timeout: 遇 timeout/connection 錯誤用長 backoff(給 VPN reconnect 時間)
        *args, **kwargs: 傳給 func

    Returns:
        func 的返回值
    Raises:
        最後一次失敗的 Exception
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_retries and is_curl_cffi_tls_error(e):
                # v6.1.20.2:timeout/connection 類錯誤用長退避(3s / 8s / 18s),給 VPN/網路重連時間
                # TLS library bug 用短退避(0.8s / 1.8s / 3.0s)
                _s = str(e)
                is_net_err = any(
                    kw in _s for kw in (
                        "timed out", "Connection reset",
                        "Connection refused", "Could not resolve",
                        "Recv failure", "connection was forcibly",
                    )
                )
                if is_net_err and long_backoff_for_timeout:
                    # 3, 8, 18 秒 + jitter — 給 VPN 時間重連
                    wait = (3 ** attempt) + 2 + random.random() * 2
                else:
                    wait = base_backoff + random.random() * 0.8 + attempt * 0.6
                if on_retry:
                    try:
                        on_retry(attempt + 1, e)
                    except Exception:
                        pass
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
