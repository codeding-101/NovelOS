"""Explicit intervals on current canonical heads, never inferred narrative chronology."""

from sqlalchemy import or_, select
from app.platform.models import MemoryEntry, MemoryProposal, MemoryStoryState


def state_data(session, proposal_id):
    state = session.get(MemoryStoryState, proposal_id)
    if state is None:
        return None
    return {
        name: getattr(state, name)
        for name in ("subject_key", "predicate", "value", "valid_from", "valid_until")
    }


def current_query(work_id):
    return (
        select(MemoryProposal)
        .join(MemoryEntry, MemoryEntry.proposal_id == MemoryProposal.id)
        .join(MemoryStoryState, MemoryStoryState.proposal_id == MemoryProposal.id)
        .where(MemoryEntry.work_id == work_id)
    )


def conflicts(session, proposal):
    state = session.get(MemoryStoryState, proposal.id)
    if state is None:
        return []
    query = current_query(proposal.work_id).where(
        MemoryProposal.entry_key != proposal.entry_key,
        MemoryStoryState.subject_key == state.subject_key,
        MemoryStoryState.predicate == state.predicate,
        or_(
            MemoryStoryState.valid_until.is_(None),
            MemoryStoryState.valid_until > state.valid_from,
        ),
    )
    if state.valid_until is not None:
        query = query.where(MemoryStoryState.valid_from < state.valid_until)
    # Even equal values overlap: one slot has one canonical owner per story instant.
    return list(session.scalars(query.order_by(MemoryProposal.entry_key).limit(20)))


def at_query(work_id, at, subject_key=None):
    query = current_query(work_id).where(
        MemoryStoryState.valid_from <= at,
        or_(MemoryStoryState.valid_until.is_(None), MemoryStoryState.valid_until > at),
    )
    if subject_key is not None:
        query = query.where(MemoryStoryState.subject_key == subject_key)
    return query.order_by(
        MemoryStoryState.subject_key,
        MemoryStoryState.predicate,
        MemoryProposal.entry_key,
    )
