import phonenumbers

from pydantic import BaseModel, field_validator


class BasePhoneValidate(BaseModel):
    phone: str | None = None

    @field_validator("phone", mode="before")
    @classmethod
    def validate_phone(cls, value):
        try:
            parsed = phonenumbers.parse(number=value, region=None)
            if not phonenumbers.is_valid_number(numobj=parsed):
                raise ValueError("Invalid phone number")
        except Exception:
            raise ValueError("Invalid phone number format")
        return phonenumbers.format_number(
            numobj=parsed, num_format=phonenumbers.PhoneNumberFormat.E164
        )
