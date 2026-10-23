from alembic import context

from app.platform.models import PlatformBase

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "Use python -m app.platform.cli upgrade to provide a validated connection"
    )
context.configure(connection=connection, target_metadata=PlatformBase.metadata)
with context.begin_transaction():
    context.run_migrations()
