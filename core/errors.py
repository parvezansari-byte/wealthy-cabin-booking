"""Translate database / auth / network errors into safe, user-facing messages.

Raw exceptions are logged server-side; users only ever see the friendly text
below, never SQL, stack traces or credentials.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

FRIENDLY = {
    # authentication / authorisation
    "AUTH_REQUIRED": "Your session has expired. Please sign in again.",
    "EMPLOYEE_NOT_AUTHORIZED": "Your account is not an active, verified employee account.",
    "ADMIN_REQUIRED": "Administrator access is required for this action.",
    "EMAIL_DOMAIN_NOT_ALLOWED": "Only official company e-mail addresses can be used.",
    "FORBIDDEN": "You are not allowed to make that change.",
    "CANNOT_CHANGE_OWN_ACCESS": "You cannot change your own access.",
    "CANNOT_CHANGE_OWN_ROLE": "You cannot change your own role.",
    "EMPLOYEE_NOT_FOUND": "Employee not found.",
    "EMPLOYEE_NOT_ACTIVE": "Only active employees can be made administrators.",
    # booking rules
    "MISSING_FIELDS": "Please fill in every required field.",
    "INVALID_TITLE": "Meeting purpose must be 3 to 120 characters.",
    "INVALID_ATTENDEES": "Number of attendees must be between 1 and 100.",
    "NOTES_TOO_LONG": "Notes must be 1000 characters or fewer.",
    "INVALID_TIME_RANGE": "Please choose a valid start time and duration.",
    "DURATION_TOO_SHORT": "That booking is shorter than the minimum duration.",
    "DURATION_TOO_LONG": "Bookings can last at most 2 hours.",
    "BOOKING_IN_PAST": "That start time has already passed. Please pick a later slot.",
    "BOOKING_TOO_FAR_AHEAD": "That date is too far ahead to book.",
    "NON_WORKING_DAY": "Cabins cannot be booked on that day.",
    "OUTSIDE_OFFICE_HOURS": "Bookings must be within office hours.",
    "CABIN_NOT_FOUND": "That cabin no longer exists.",
    "CABIN_INACTIVE": "That cabin is currently unavailable.",
    "CAPACITY_EXCEEDED": "Too many attendees for this cabin.",
    "CABIN_UNDER_MAINTENANCE": "That cabin is blocked for maintenance at that time.",
    "CABIN_ALREADY_BOOKED": "Sorry — someone has already booked this cabin for part of that time.",
    # cancellation
    "BOOKING_NOT_FOUND": "Booking not found.",
    "ALREADY_CANCELLED": "This booking is already cancelled.",
    "BOOKING_ALREADY_ENDED": "Past bookings cannot be cancelled.",
    "CANCELLATION_WINDOW_CLOSED": "This booking can no longer be cancelled.",
    # admin
    "INVALID_RANGE": "Please choose a shorter date range.",
    "INVALID_STATUS": "Invalid status.",
    "INVALID_ROLE": "Invalid role.",
    "INVALID_REASON": "Reason must be 3 to 200 characters.",
    "MAINTENANCE_IN_PAST": "Maintenance must end in the future.",
    "MAINTENANCE_CONFLICTS_BOOKINGS": "Confirmed bookings overlap this period.",
    "MAINTENANCE_NOT_FOUND": "That maintenance block was not found.",
    "INVALID_TIMEZONE": "Unknown time zone.",
}

GENERIC = "Something went wrong while talking to the server. Please try again in a moment."


class AppError(Exception):
    """An error that is safe to show to the user."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or FRIENDLY.get(code, GENERIC)
        super().__init__(f"{code}: {self.message}")


def from_postgrest(exc: Exception) -> AppError:
    """Map a postgrest.exceptions.APIError (or anything else) to AppError."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or ""
    details = getattr(exc, "details", None)

    if message in FRIENDLY:
        # Our own functions put a human sentence in DETAIL; prefer it when present.
        text = details if isinstance(details, str) and 0 < len(details) <= 300 else FRIENDLY[message]
        return AppError(message, text)
    if code == "23P01":  # exclusion_violation that escaped the function wrapper
        return AppError("CABIN_ALREADY_BOOKED")
    if code in ("PGRST301", "PGRST302", "PGRST303") or "JWT" in message:
        return AppError("AUTH_REQUIRED")
    if code == "42501":
        return AppError("FORBIDDEN")
    log.error("Unexpected database error code=%s message=%s", code, message)
    return AppError("UNEXPECTED", GENERIC)


AUTH_MESSAGES = {
    "invalid_credentials": "Incorrect e-mail or password.",
    "email_not_confirmed": "Please verify your e-mail address first. Use the code we e-mailed you.",
    "user_already_exists": "An account already exists for this e-mail. Please sign in.",
    "email_exists": "An account already exists for this e-mail. Please sign in.",
    "weak_password": "Please choose a stronger password.",
    "over_request_rate_limit": "Too many attempts. Please wait a few minutes and try again.",
    "over_email_send_rate_limit": "Too many e-mails were sent. Please wait a few minutes.",
    "otp_expired": "That code is invalid or has expired. Request a new one.",
    "same_password": "The new password must be different from the old one.",
    "user_banned": "This account has been disabled.",
    "signup_disabled": "Registration is currently disabled. Contact your administrator.",
}


def from_auth(exc: Exception) -> AppError:
    code = getattr(exc, "code", None) or ""
    message = (getattr(exc, "message", None) or str(exc) or "").lower()
    if code in AUTH_MESSAGES:
        return AppError(code, AUTH_MESSAGES[code])
    if "email not confirmed" in message:
        return AppError("email_not_confirmed", AUTH_MESSAGES["email_not_confirmed"])
    if "invalid login" in message:
        return AppError("invalid_credentials", AUTH_MESSAGES["invalid_credentials"])
    if "database error" in message:  # e.g. our auth.users domain trigger refused the address
        return AppError("EMAIL_DOMAIN_NOT_ALLOWED")
    if "rate limit" in message:
        return AppError("over_request_rate_limit", AUTH_MESSAGES["over_request_rate_limit"])
    if "expired" in message or "invalid" in message and "token" in message:
        return AppError("otp_expired", AUTH_MESSAGES["otp_expired"])
    log.warning("Unmapped auth error code=%s", code)
    return AppError("AUTH_ERROR", "We couldn't complete that request. Please try again.")
