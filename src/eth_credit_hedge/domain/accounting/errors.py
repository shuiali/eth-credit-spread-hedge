"""Errors raised while validating immutable accounting contracts."""


class AccountingContractError(ValueError):
    """An accounting fact violates the M2.1 contract."""


class DuplicateAccountingIdentifierError(AccountingContractError):
    """A caller supplied the same identifier with different content."""
