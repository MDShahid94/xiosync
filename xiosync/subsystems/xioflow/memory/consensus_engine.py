from __future__ import annotations

import logging

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from xiosync.subsystems.xioflow.models.consensus import XioflowConsensusVote
from xiosync.subsystems.xioflow.models.memory_nodes import XioflowMemoryNode

logger = logging.getLogger(__name__)

class ConsensusEngine:
    """Bayesian EMA voting and tier promotion."""

    def __init__(self, session: Session) -> None:
        """Initialize with a SQLAlchemy session."""
        self.session = session
        self.settings = {
            'ema_alpha': 0.3,
            'prior_weight': 5.0,
            'prior_mean': 0.5,
            'promote_threshold': 0.75,
            'demote_threshold': 0.25,
            'archive_threshold': 0.1
        }

    @staticmethod
    def calculate_bayesian_ema(
        existing_score: float,
        raw_vote: float,
        tier_confidence: float,
        vote_weight: float,
        settings: dict
    ) -> tuple[float, float, float]:
        """Calculates bayesian score, new ema, and new weight."""
        alpha = settings['ema_alpha']
        new_ema = alpha * (raw_vote * tier_confidence) + (1 - alpha) * existing_score
        new_weight = vote_weight + abs(raw_vote)

        prior_weight = settings['prior_weight']
        prior_mean = settings['prior_mean']

        bayesian = (new_weight * new_ema + prior_weight * prior_mean) / (new_weight + prior_weight)
        return (bayesian, new_ema, new_weight)

    def submit_vote(
        self,
        org_id: str,
        node_id: str,
        voter_id: str,
        raw_vote: float,
        tier_confidence: float = 1.0,
        winning_tier: str | None = None,
        context_hash: str | None = None
    ) -> None:
        """Insert vote, recalculate Bayesian scores, and auto-promote/demote."""
        stmt = insert(XioflowConsensusVote).values(
            org_id=org_id,
            node_id=node_id,
            voter_id=voter_id,
            raw_vote=raw_vote,
            tier_confidence=tier_confidence
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=['node_id', 'voter_id'],
            set_={'raw_vote': raw_vote, 'tier_confidence': tier_confidence}
        )
        self.session.execute(stmt)

        node = self.session.query(XioflowMemoryNode).filter_by(id=node_id, org_id=org_id).first()
        if not node:
            return

        existing_score = getattr(node, 'ema_score', 0.5)
        vote_weight = getattr(node, 'vote_weight', 0.0)

        bayesian, new_ema, new_weight = self.calculate_bayesian_ema(
            existing_score, raw_vote, tier_confidence, vote_weight, self.settings
        )

        node.bayesian_score = bayesian
        node.ema_score = new_ema
        node.vote_weight = new_weight

        if node.tier == 'platform_global':
            self.session.commit()
            return

        if bayesian > self.settings['promote_threshold'] and new_weight > 5.0:
            if node.tier == 'project_experimental':
                node.tier = 'project_ground_truth'
            elif node.tier == 'project_ground_truth':
                node.tier = 'organization_shared'
        elif bayesian < self.settings['demote_threshold']:
            if node.tier == 'organization_shared':
                node.tier = 'project_ground_truth'
            elif node.tier == 'project_ground_truth':
                node.tier = 'project_experimental'

        if bayesian < self.settings['archive_threshold']:
            node.status = 'ARCHIVED'

        self.session.commit()
