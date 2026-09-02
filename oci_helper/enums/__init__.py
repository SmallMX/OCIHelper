"""OCI Helper enums.

The Python migration originally imported Java-style ``*Enum`` names that did
not exist.  Export the canonical Python class names and keep explicit aliases
for callers that still use the old migration names.
"""

from enums.architecture import Architecture, BillingType
from enums.operation_system import OperationSystem
from enums.security_rule import SecurityRuleProtocol

ArchitectureEnum = Architecture
OperationSystemEnum = OperationSystem
SecurityRuleProtocolEnum = SecurityRuleProtocol

__all__ = [
    "Architecture",
    "ArchitectureEnum",
    "BillingType",
    "OperationSystem",
    "OperationSystemEnum",
    "SecurityRuleProtocol",
    "SecurityRuleProtocolEnum",
]
