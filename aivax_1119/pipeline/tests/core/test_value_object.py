from src.core.ddd import ValueObject


class _TestValueObject(ValueObject):
    def __init__(self) -> None:
        self.validated = False
        super().__init__()

    def validate(self) -> None:
        self.validated = True


def test_value_object_constructor_runs_validate() -> None:
    value_object = _TestValueObject()

    assert value_object.validated is True
