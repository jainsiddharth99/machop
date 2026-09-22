"""The v0.1 rate limiter locked out the *correct* PIN permanently."""

import pytest

from machop.security import AuthError, SessionAuth, generate_pin


def test_generate_pin_shape():
    pin = generate_pin()
    assert len(pin) == 6 and pin.isdigit()


def test_generated_pins_differ():
    assert len({generate_pin() for _ in range(50)}) > 40


def test_correct_pin_claims_session():
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "device-a")
    assert auth.claimed


def test_wrong_pin_rejected():
    auth = SessionAuth(pin="123456")
    with pytest.raises(AuthError) as exc:
        auth.authenticate("000000", "device-a")
    assert exc.value.status == 401


def test_correct_pin_still_works_after_failures():
    """v0.1 returned 429 forever once three attempts failed, even for the right code."""
    auth = SessionAuth(pin="123456")
    for _ in range(4):
        with pytest.raises(AuthError):
            auth.authenticate("999999", "device-a")
    auth.authenticate("123456", "device-a")
    assert auth.claimed


def test_backoff_engages_then_expires():
    auth = SessionAuth(pin="123456", max_attempts=3)
    for _ in range(3):
        with pytest.raises(AuthError):
            auth.authenticate("999999", "d", now=100.0)

    with pytest.raises(AuthError) as exc:
        auth.authenticate("123456", "d", now=100.5)
    assert exc.value.status == 429 and exc.value.retry_after > 0

    auth.authenticate("123456", "d", now=400.0)
    assert auth.claimed


def test_pin_does_not_expire_by_default():
    """The whole point: start it, leave the house, connect hours later."""
    auth = SessionAuth(pin="123456", created_at=0.0)
    auth.authenticate("123456", "phone", now=86_400.0)
    assert auth.claimed


def test_explicit_ttl_still_expires():
    auth = SessionAuth(pin="123456", ttl=300.0, created_at=0.0)
    with pytest.raises(AuthError) as exc:
        auth.authenticate("123456", "phone", now=301.0)
    assert exc.value.status == 410


def test_claimed_session_does_not_expire():
    auth = SessionAuth(pin="123456", ttl=300.0, created_at=0.0)
    auth.authenticate("123456", "d", now=10.0)
    auth.authenticate("123456", "d", now=9_999.0)


def test_same_device_can_reconnect_after_a_dropped_connection():
    """Cellular drops constantly; that must not be a lockout."""
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "phone")
    auth.release()
    auth.authenticate("123456", "phone")
    assert auth.claimed


def test_a_second_device_with_the_code_takes_the_session_over():
    """This used to be a 409, and the 409 was the wrong call."""
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "device-a")
    displaced = auth.authenticate("123456", "device-b")
    assert displaced is True, "the caller must know to hang up on device-a"
    assert auth._claimed_by == "device-b"


def test_taking_over_still_needs_the_right_code():
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "device-a")
    with pytest.raises(AuthError) as exc:
        auth.authenticate("000000", "device-b")
    assert exc.value.status == 401
    assert auth._claimed_by == "device-a", "a wrong code must not evict anyone"


def test_a_wrong_code_from_a_second_device_is_throttled_normally():
    """Otherwise takeover would be a way to brute force without a backoff."""
    auth = SessionAuth(pin="123456", max_attempts=2)
    auth.authenticate("123456", "device-a")
    for _ in range(2):
        with pytest.raises(AuthError):
            auth.authenticate("000000", "device-b")
    with pytest.raises(AuthError) as exc:
        auth.authenticate("123456", "device-b")
    assert exc.value.status == 429


def test_reconnecting_as_the_same_device_displaces_nobody():
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "device-a")
    assert auth.authenticate("123456", "device-a") is False


def test_claiming_a_free_session_displaces_nobody():
    auth = SessionAuth(pin="123456")
    assert auth.authenticate("123456", "device-a") is False


def test_release_allows_a_new_device():
    auth = SessionAuth(pin="123456")
    auth.authenticate("123456", "device-a")
    auth.release()
    auth.authenticate("123456", "device-b")


def test_resume_lets_the_same_device_back_in_without_the_pin():
    auth = SessionAuth(pin="424242")
    auth.authenticate("424242", "device-a")
    auth.release()
    auth.resume("device-a")
    assert auth.claimed


def test_resume_refuses_a_device_that_never_had_the_session():
    auth = SessionAuth(pin="424242")
    auth.authenticate("424242", "device-a")
    auth.release()
    with pytest.raises(AuthError) as excinfo:
        auth.resume("device-b")
    assert excinfo.value.status == 401


def test_resume_refuses_before_anyone_has_authenticated():
    """Otherwise a made-up cookie would be a way in with no code at all."""
    auth = SessionAuth(pin="424242")
    with pytest.raises(AuthError):
        auth.resume("device-a")


def test_resume_refuses_an_empty_session_id():
    auth = SessionAuth(pin="424242")
    auth.authenticate("424242", "device-a")
    auth.release()
    with pytest.raises(AuthError):
        auth.resume("")


def test_resume_loses_to_a_device_that_has_since_claimed_it():
    """Whoever last proved they know the code is the holder; the previous device has to type it again rather than silently stealing it back."""
    auth = SessionAuth(pin="424242")
    auth.authenticate("424242", "device-a")
    auth.release()
    auth.authenticate("424242", "device-b")
    with pytest.raises(AuthError) as excinfo:
        auth.resume("device-a")
    assert excinfo.value.status == 401
    assert auth._claimed_by == "device-b", "the new holder must keep it"


def test_resume_is_idempotent_for_the_holder():
    auth = SessionAuth(pin="424242")
    auth.authenticate("424242", "device-a")
    auth.resume("device-a")
    auth.resume("device-a")
    assert auth.claimed


def test_resume_does_not_consume_a_failed_attempt():
    """There is nothing to guess in a 144-bit token, so throttling it would only ever lock out the legitimate device."""
    auth = SessionAuth(pin="424242", max_attempts=2)
    auth.authenticate("424242", "device-a")
    auth.release()
    for _ in range(10):
        with pytest.raises(AuthError):
            auth.resume("device-b")
    auth.authenticate("424242", "device-a")
    assert auth.claimed


def test_a_used_session_resumes_even_past_its_ttl():
    """The TTL bounds how long an UNUSED code is worth something."""
    auth = SessionAuth(pin="424242", ttl=1.0)
    auth.authenticate("424242", "device-a", now=0.0)
    auth.release()
    auth.resume("device-a")
    assert auth.claimed
