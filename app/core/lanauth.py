"""LANモードの PIN 認証（P5契約 §2）。

Gradio の `demo.launch(auth=...)` へ渡す callable を提供する。
Gradio 6.22.0 では `auth=` が UI だけでなく `/config`・`/gradio_api/info`・
`/gradio_api/run/...`・`/gradio_api/queue/join`・`/gradio_api/file=...` まで保護する
（未認証は 401）。これが LANモードの土台。

**PIN の取り扱い（絶対規則）**
- PIN は `PinAuthenticator` の内部とターミナル表示だけに存在させる
- config・履歴・ログ・URL・QR・プロセス引数・環境変数・`LanInfo` へ入れない
- `repr()` / `str()` でも漏らさない（`__repr__` を伏字にしている）
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable

# iPhone のログイン画面に表示する固定ユーザー名（秘密ではない。秘密は PIN だけ）
LAN_USERNAME = "h3"

MIN_PIN_DIGITS = 4
MAX_PIN_DIGITS = 12


def generate_pin(digits: int = 6) -> str:
    """暗号学的乱数で PIN を作る（先頭ゼロを許容するため文字単位で生成）。"""
    if not isinstance(digits, int) or isinstance(digits, bool):
        raise ValueError("PINの桁数は整数で指定してください")
    if not (MIN_PIN_DIGITS <= digits <= MAX_PIN_DIGITS):
        raise ValueError(
            f"PINの桁数は {MIN_PIN_DIGITS}〜{MAX_PIN_DIGITS} の範囲で指定してください"
        )
    return "".join(str(secrets.randbelow(10)) for _ in range(digits))


class PinAuthenticator:
    """PIN の定数時間照合＋連続失敗のロックアウト。

    - PIN は平文で保持しない。起動ごとのランダムsaltで HMAC-SHA256 した
      **固定長ダイジェストだけ**を持つ（`vars()`・デバッガ・メモリダンプにも平文が出ない）
    - 照合は `hmac.compare_digest`（長さ・内容をタイミングから推測されにくくする）
    - ユーザー名は任意（無視する）。秘密は PIN だけ
    - `max_failures` 回連続で失敗すると `lockout_sec` 秒だけすべて拒否する
    - 成功すると失敗カウントを 0 に戻す
    - スレッド安全（Gradio は複数スレッドから認証を呼ぶ）
    """

    def __init__(
        self,
        pin: str,
        *,
        max_failures: int = 10,
        lockout_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(pin, str) or not pin.strip():
            raise ValueError("PINが空です")
        if not isinstance(max_failures, int) or isinstance(max_failures, bool):
            raise ValueError("max_failures は整数で指定してください")
        if max_failures < 1:
            raise ValueError("max_failures は 1 以上で指定してください")
        if lockout_sec < 0:
            raise ValueError("lockout_sec は 0 以上で指定してください")
        # 平文は保持しない。salt 付きダイジェストだけを保持する
        self._salt = secrets.token_bytes(16)
        self._expected = self._digest(pin)
        self._max_failures = max_failures
        self._lockout_sec = float(lockout_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until: float | None = None
        self._lockouts = 0

    # ------------------------------------------------------------ 照合

    def _digest(self, value: str) -> bytes:
        return hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256).digest()

    def check(self, username: str, password: str) -> bool:
        """PIN が一致すれば True。ロックアウト中は常に False。"""
        with self._lock:
            now = self._clock()
            if self._locked_until is not None:
                if now < self._locked_until:
                    return False
                # ロック解除（次の窓で再び max_failures 回まで試せる）
                self._locked_until = None
                self._failures = 0

            candidate = password if isinstance(password, str) else ""
            ok = hmac.compare_digest(self._digest(candidate), self._expected)
            if ok:
                self._failures = 0
                return True

            self._failures += 1
            if self._failures >= self._max_failures and self._lockout_sec > 0:
                self._locked_until = now + self._lockout_sec
                self._lockouts += 1
            return False

    def as_gradio_auth(self) -> Callable[[str, str], bool]:
        """`demo.launch(auth=...)` へ渡す callable を返す。"""

        def _auth(username: str, password: str) -> bool:
            return self.check(username, password)

        return _auth

    # ------------------------------------------------------------ 状態

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failures

    @property
    def lockout_count(self) -> int:
        """ロックアウトが発生した回数（監視・テスト用。PIN は含まない）。"""
        with self._lock:
            return self._lockouts

    def locked_for(self) -> float:
        """残りロック秒。ロックされていなければ 0.0。"""
        with self._lock:
            if self._locked_until is None:
                return 0.0
            remaining = self._locked_until - self._clock()
            return remaining if remaining > 0 else 0.0

    def is_locked(self) -> bool:
        return self.locked_for() > 0.0

    # ------------------------------------------------------------ 伏字

    def __repr__(self) -> str:  # PIN を絶対に出さない
        return (
            f"<PinAuthenticator pin=**** failures={self._failures} "
            f"locked={self._locked_until is not None}>"
        )

    __str__ = __repr__
