"""Compatibility imports for the single shared schema, without database side effects."""
from db import Admin, Base, BroadcastJob, BroadcastRecipient, Client, QuarantinedRow, Setting

__all__ = ['Admin', 'Base', 'BroadcastJob', 'BroadcastRecipient', 'Client', 'QuarantinedRow', 'Setting']
