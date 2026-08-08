"""PIN 認証のユニットテスト（P5契約 §2）。

PIN が `PinAuthenticator` の外へ漏れないこと（repr・str・例外・属性）を含む。
"""

from __future__ import annotations

import threading

import pytest

from app.core.lanauth import (
    LAN_USERNAME,
    MAX_PIN_DIGITS,
    MIN_PIN_DIGITS,
    PinAuthenticator,
    generate_pin,
)


class FakeClock:
    """単調増加の擬似時計（実時間を待たずロックアウトを検証する）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------- PIN 生成


def test_generate_pin_default_is_six_digits():
    pin = generate_pin()
    assert len(pin) == 6
    assert pin.isdigit()


@pytest.mark.parametrize("digits", [4, 5, 6, 8, 12])
def test_generate_pin_respects_digit_count(digits: int):
    pin = generate_pin(digits)
    assert len(pin) == digits and pin.isdigit()


def test_generate_pin_allows_leading_zero():
    """先頭ゼロを許容する（int 経由で作ると桁が減るため）。"""
    pins = [generate_pin(6) for _ in range(4000)]
    assert all(len(p) == 6 for p in pins)
    assert any(p.startswith("0") for p in pins)


def test_generate_pin_is_not_constant():
    assert len({generate_pin() for _ in range(50)}) > 25


@pytest.mark.parametrize("digits", [0, 1, 3, 13, 100, -1])
def test_generate_pin_rejects_out_of_range(digits: int):
    with pytest.raises(ValueError):
        generate_pin(digits)


def test_generate_pin_rejects_non_int():
    for bad in ("6", 6.0, None, True):
        with pytest.raises(ValueError):
            generate_pin(bad)  # type: ignore[arg-type]


def test_digit_bounds_are_documented_constants():
    assert (MIN_PIN_DIGITS, MAX_PIN_DIGITS) == (4, 12)


def test_lan_username_is_fixed():
    assert LAN_USERNAME == "h3"


# ---------------------------------------------------------------- 照合


def test_correct_pin_is_accepted():
    auth = PinAuthenticator("123456")
    assert auth.check(LAN_USERNAME, "123456") is True


def test_wrong_pin_is_rejected():
    auth = PinAuthenticator("123456")
    assert auth.check(LAN_USERNAME, "123457") is False
    assert auth.check(LAN_USERNAME, "") is False
    assert auth.check(LAN_USERNAME, "1234567") is False
    assert auth.check(LAN_USERNAME, "12345") is False


def test_username_is_ignored():
    auth = PinAuthenticator("123456")
    for name in ("h3", "", "admin", "だれか", None):
        assert auth.check(name, "123456") is True  # type: ignore[arg-type]


def test_non_string_password_is_rejected_without_crash():
    auth = PinAuthenticator("123456")
    for bad in (None, 123456, b"123456", ["123456"]):
        assert auth.check(LAN_USERNAME, bad) is False  # type: ignore[arg-type]


def test_non_ascii_password_does_not_raise():
    """hmac.compare_digest は非ASCIIのstrで TypeError を出す。utf-8 で比較する。"""
    auth = PinAuthenticator("123456")
    assert auth.check(LAN_USERNAME, "パスワード") is False


def test_empty_pin_is_rejected_at_construction():
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="PIN"):
            PinAuthenticator(bad)
    for bad in (None, 123456):
        with pytest.raises(ValueError):
            PinAuthenticator(bad)  # type: ignore[arg-type]


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        PinAuthenticator("123456", max_failures=0)
    with pytest.raises(ValueError):
        PinAuthenticator("123456", lockout_sec=-1)


# ---------------------------------------------------------------- ロックアウト


def test_lockout_after_max_failures():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=3, lockout_sec=30.0, clock=clock)

    assert auth.check("h3", "000000") is False
    assert auth.check("h3", "000000") is False
    assert auth.failure_count == 2
    assert auth.locked_for() == 0.0

    assert auth.check("h3", "000000") is False   # 3回目でロック
    assert auth.failure_count == 3
    assert auth.locked_for() == pytest.approx(30.0)
    assert auth.is_locked() is True

    # ロック中は正しい PIN でも拒否する
    assert auth.check("h3", "123456") is False


def test_lockout_expires_and_allows_retry():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=2, lockout_sec=30.0, clock=clock)
    auth.check("h3", "x")
    auth.check("h3", "x")
    assert auth.is_locked() is True

    clock.advance(29.0)
    assert auth.check("h3", "123456") is False   # まだロック中
    assert auth.locked_for() == pytest.approx(1.0)

    clock.advance(1.5)
    assert auth.locked_for() == 0.0
    assert auth.check("h3", "123456") is True    # 解除後は通る
    assert auth.failure_count == 0


def test_success_resets_failure_count():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=3, lockout_sec=30.0, clock=clock)
    auth.check("h3", "x")
    auth.check("h3", "x")
    assert auth.failure_count == 2
    assert auth.check("h3", "123456") is True
    assert auth.failure_count == 0
    # リセット後はまた max_failures 回まで試せる
    auth.check("h3", "x")
    auth.check("h3", "x")
    assert auth.is_locked() is False


def test_repeated_lockouts_are_counted():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=2, lockout_sec=10.0, clock=clock)
    for _ in range(3):
        auth.check("h3", "x")
        auth.check("h3", "x")
        assert auth.is_locked()
        clock.advance(11.0)
    assert auth.lockout_count == 3


def test_lockout_disabled_when_lockout_sec_is_zero():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=2, lockout_sec=0.0, clock=clock)
    auth.check("h3", "x")
    auth.check("h3", "x")
    assert auth.is_locked() is False
    assert auth.check("h3", "123456") is True


def test_default_limits_match_contract():
    import inspect

    sig = inspect.signature(PinAuthenticator.__init__)
    assert sig.parameters["max_failures"].default == 10
    assert sig.parameters["lockout_sec"].default == 30.0


# ---------------------------------------------------------------- Gradio 連携


def test_as_gradio_auth_returns_callable_with_two_args():
    auth = PinAuthenticator("123456")
    fn = auth.as_gradio_auth()
    assert callable(fn)
    assert fn("h3", "123456") is True
    assert fn("h3", "999999") is False


def test_gradio_auth_shares_lockout_state():
    clock = FakeClock()
    auth = PinAuthenticator("123456", max_failures=2, lockout_sec=30.0, clock=clock)
    fn = auth.as_gradio_auth()
    fn("h3", "x")
    fn("h3", "x")
    assert auth.is_locked() is True
    assert fn("h3", "123456") is False


def test_gradio_auth_callable_does_not_expose_pin():
    auth = PinAuthenticator("987654")
    fn = auth.as_gradio_auth()
    assert "987654" not in repr(fn)
    for cell in (fn.__closure__ or ()):
        assert cell.cell_contents is not "987654"  # noqa: F632  （同一オブジェクト比較）


# ---------------------------------------------------------------- PIN を漏らさない


def test_repr_and_str_mask_the_pin():
    auth = PinAuthenticator("135791")
    assert "135791" not in repr(auth)
    assert "135791" not in str(auth)
    assert "135791" not in f"{auth}"
    assert "135791" not in "{}".format(auth)
    assert "****" in repr(auth)


def test_repr_does_not_leak_pin_length():
    short = PinAuthenticator("1234")
    long = PinAuthenticator("123456789012")
    assert repr(short).replace("failures=0", "") == repr(long).replace("failures=0", "")


def test_instance_dict_has_no_plaintext_pin():
    auth = PinAuthenticator("246810")
    dumped = repr(vars(auth))
    assert "246810" not in dumped
    assert "'246810'" not in dumped


def test_pin_is_not_exposed_as_public_attribute():
    auth = PinAuthenticator("246810")
    public = [n for n in dir(auth) if not n.startswith("_")]
    for name in public:
        value = getattr(auth, name)
        if callable(value):
            continue
        assert "246810" not in str(value)


def test_exception_paths_do_not_include_pin():
    auth = PinAuthenticator("246810")
    try:
        auth.check("h3", object())  # type: ignore[arg-type]
    except Exception as e:  # 例外を投げない設計だが、投げても PIN は載せない
        assert "246810" not in str(e)


# ---------------------------------------------------------------- 並行性


def test_check_is_thread_safe():
    auth = PinAuthenticator("123456", max_failures=10_000, lockout_sec=30.0)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(ok: bool) -> None:
        for _ in range(200):
            got = auth.check("h3", "123456" if ok else "000000")
            with lock:
                results.append(got)

    threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8 * 200
    assert results.count(True) == 4 * 200
