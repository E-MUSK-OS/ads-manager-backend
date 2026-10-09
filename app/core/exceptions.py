class ServiceValidationError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
