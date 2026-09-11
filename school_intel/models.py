"""SQLAlchemy 2.0 models mirroring docs/DATA-MODEL.md.

The DDL in that document is the specification. Same table names, same column
names, same constraints, same partial indexes. Deviating needs an ADR.

Three layers (ADR-009):
  Layer 0  source_registry
  Layer 1  fetches, raw_documents                    - immutable, append-only
  Layer 2  observations                              - append-only claims
  Layer 3  institutions, groups, people, ...         - derived, disposable

`make rebuild` truncates only layer 3. Never delete from layers 1 and 2.
"""

from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Layer 3 tables, in the order `make rebuild` truncates them. Kept next to the
# models so the rebuild contract cannot drift from the schema (M1-6).
CANONICAL_TABLES = (
    "scores",
    "roles",
    "people",
    "institution_boards",
    "institutions",
    "groups",
)

# Never truncated, never deleted from. ADR-009, hard rule 4.
APPEND_ONLY_TABLES = (
    "observations",
    "fetches",
    "raw_documents",
    "signals",
    "review_queue",
    "overrides",
    "merge_decisions",
    "eval_gold",
)


# --------------------------------------------------------------------------
# Layer 0 - source registry
# --------------------------------------------------------------------------


class SourceRegistry(Base):
    __tablename__ = "source_registry"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # 1=govt/statutory 2=board/official 3=institution self-published 4=third party.
    # Default tie-break in conflict resolution; per-field overrides in docs/SOURCES.md.
    authority_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    robots_ok: Mapped[bool | None] = mapped_column(Boolean)
    tos_note: Mapped[str | None] = mapped_column(Text)
    refresh_days: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_limit_rps: Mapped[float] = mapped_column(
        Numeric, nullable=False, server_default=text("1.0")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


# --------------------------------------------------------------------------
# Layer 1 - raw. Immutable, append-only.
# --------------------------------------------------------------------------


class Fetch(Base):
    __tablename__ = "fetches"
    __table_args__ = (
        Index("ix_fetches_url_requested", "url", text("requested_at DESC")),
        Index("ix_fetches_source_requested", "source_id", text("requested_at DESC")),
        Index("ix_fetches_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_registry.id"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    robots_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Non-NULL means the ADR-006 gate blocked it and no request was made.
    denylist_hit: Mapped[str | None] = mapped_column(Text)


class RawDocument(Base):
    __tablename__ = "raw_documents"

    content_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --------------------------------------------------------------------------
# Layer 2 - observations. Append-only claims. The most important table.
# --------------------------------------------------------------------------


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_observations_confidence"
        ),
        Index("ix_observations_entity_field", "entity_key", "field"),
        Index(
            "ix_observations_institution_field",
            "institution_id",
            "field",
            postgresql_where=text("institution_id IS NOT NULL"),
        ),
        Index("ix_observations_hash_extractor", "content_hash", "extractor"),
        # Makes re-extraction idempotent: same document + extractor + field = one
        # live row. Alembic autogenerate misses partial indexes; this one is
        # load-bearing, so it is asserted in test_migrations.
        Index(
            "uq_observations_live",
            "content_hash",
            "extractor",
            "entity_key",
            "field",
            unique=True,
            postgresql_where=text("superseded_by IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # subject
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Natural key from the source, assigned before resolution: 'cbse:330801'.
    entity_key: Mapped[str] = mapped_column(Text, nullable=False)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institutions.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))

    # claim
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[float | None] = mapped_column(Numeric)
    value_json: Mapped[dict | None] = mapped_column(JSONB)
    unit: Mapped[str | None] = mapped_column(Text)

    # provenance
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_registry.id"), nullable=False
    )
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetches.id"))
    content_hash: Mapped[str | None] = mapped_column(
        ForeignKey("raw_documents.content_hash")
    )
    # Verbatim source text supporting the claim. Powers /institutions/{id}/why.
    # An extraction with no evidence span is a bug, at every tier.
    evidence_span: Mapped[str | None] = mapped_column(Text)
    # When the SOURCE says it was true, not when we read it. Freshness uses this.
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Embeds the parser/prompt version: 'parser:cbse_saras_v3', 'ocr:rapidocr_v1'.
    extractor: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric, nullable=False)

    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("observations.id"))


# Canonical field vocabulary. Adding one requires updating resolve/conflict.py
# and docs/SOURCES.md in the same change.
# fmt: off
OBSERVATION_FIELDS = frozenset(
    # Grouped exactly as docs/DATA-MODEL.md lists them, so the two are diffable.
    ["name", "legal_entity_name", "institution_type"]
    + ["address", "pincode", "city", "district", "state", "metro_area"]
    + [
        "grade_low",
        "grade_high",
        "has_class_12",
        "streams",
        "medium",
        "gender",
        "residential",
    ]
    + ["board", "board_status", "board_valid_from", "board_valid_to"]
    + ["fee_annual_inr", "fee_year", "fee_component_breakdown"]
    + [
        "total_enrollment",
        "enrollment_year",
        "class_12_total",
        "pcm_12_count",
        "pcm_12_count_year",
        # Staff count. Context for BD, and the basis for an approximate student
        # count when none is published (docs/SOURCES.md priority table).
        "total_teachers",
    ]
    + ["website", "email", "phone"]
    + ["principal_name", "principal_title", "counsellor_name", "counsellor_title"]
    + ["management_type", "year_founded", "status"]
    + ["group_name", "group_campus_count"]
)
# fmt: on


# --------------------------------------------------------------------------
# Layer 3 - canonical. Derived, disposable.
# --------------------------------------------------------------------------


class Institution(Base):
    __tablename__ = "institutions"
    __table_args__ = (
        Index("ix_institutions_state_city", "state", "city"),
        Index("ix_institutions_pincode", "pincode"),
        Index(
            "ix_institutions_metro",
            "metro_area",
            postgresql_where=text("metro_area IS NOT NULL"),
        ),
        Index(
            "ix_institutions_group",
            "group_id",
            postgresql_where=text("group_id IS NOT NULL"),
        ),
        Index(
            "ix_institutions_legal_entity_norm",
            "legal_entity_norm",
            postgresql_where=text("legal_entity_norm IS NOT NULL"),
        ),
        Index("ix_institutions_search_tsv", "search_tsv", postgresql_using="gin"),
        Index(
            "ix_institutions_name_trgm",
            "canonical_name",
            postgresql_using="gin",
            postgresql_ops={"canonical_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_institutions_has_class_12",
            "has_class_12",
            postgresql_where=text("has_class_12 = true"),
        ),
        # Sort keys. NULLS LAST here matches the mandatory query-side NULLS LAST
        # (M5-1), so the planner serves a sorted page from the index.
        Index(
            "ix_institutions_fee_mid", text("fee_annual_inr_mid DESC NULLS LAST"), "id"
        ),
        Index(
            "ix_institutions_fee_min", text("fee_annual_inr_min DESC NULLS LAST"), "id"
        ),
        Index("ix_institutions_pcm_12", text("pcm_12_count DESC NULLS LAST"), "id"),
        Index("ix_institutions_class_12", text("class_12_total DESC NULLS LAST"), "id"),
        Index(
            "ix_institutions_enrollment", text("total_enrollment DESC NULLS LAST"), "id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Stable external identifiers - the backbone of entity resolution (ADR-014).
    udise_code: Mapped[str | None] = mapped_column(Text, unique=True)
    cbse_affiliation_no: Mapped[str | None] = mapped_column(Text, unique=True)
    cisce_code: Mapped[str | None] = mapped_column(Text, unique=True)
    ib_school_code: Mapped[str | None] = mapped_column(Text, unique=True)
    cambridge_centre_no: Mapped[str | None] = mapped_column(Text, unique=True)

    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    institution_type: Mapped[str] = mapped_column(Text, nullable=False)

    # geography
    address: Mapped[str | None] = mapped_column(Text)
    pincode: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    metro_area: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column()
    lon: Mapped[float | None] = mapped_column()
    geo_precision: Mapped[str | None] = mapped_column(Text)

    # offering - the hard gate lives here
    grade_low: Mapped[int | None] = mapped_column(SmallInteger)
    grade_high: Mapped[int | None] = mapped_column(SmallInteger)
    has_class_12: Mapped[bool | None] = mapped_column(Boolean)
    streams: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    medium: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    gender: Mapped[str | None] = mapped_column(Text)
    residential: Mapped[str | None] = mapped_column(Text)

    # commercial - ranges, per Project-Doc 6.7
    fee_annual_inr_min: Mapped[int | None] = mapped_column(Integer)
    fee_annual_inr_max: Mapped[int | None] = mapped_column(Integer)
    # Generated, so the fee-sorted list and the affordability component can never
    # disagree about what "the fee" is. Do not recompute a midpoint in Python.
    fee_annual_inr_mid: Mapped[int | None] = mapped_column(
        Integer,
        Computed(
            "COALESCE((fee_annual_inr_min + fee_annual_inr_max) / 2, "
            "fee_annual_inr_min, fee_annual_inr_max)",
            persisted=True,
        ),
    )
    fee_year: Mapped[int | None] = mapped_column(SmallInteger)
    total_enrollment: Mapped[int | None] = mapped_column(Integer)
    enrollment_year: Mapped[int | None] = mapped_column(SmallInteger)
    #: Published teacher count. Context for the sales team, and the basis for
    #: an approximate student count when none is published.
    total_teachers: Mapped[int | None] = mapped_column(Integer)
    #: True when total_enrollment was derived rather than published, so it can
    #: be rendered as "~N (estimated)" and never mistaken for a stated figure.
    enrollment_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    class_12_total: Mapped[int | None] = mapped_column(Integer)
    pcm_12_count: Mapped[int | None] = mapped_column(Integer)
    pcm_12_count_year: Mapped[int | None] = mapped_column(SmallInteger)
    pcm_12_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # contact
    website: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)

    # org
    group_id: Mapped[int | None] = mapped_column(ForeignKey("groups.id"))
    legal_entity_name: Mapped[str | None] = mapped_column(Text)
    legal_entity_norm: Mapped[str | None] = mapped_column(Text)
    management_type: Mapped[str | None] = mapped_column(Text)

    # lifecycle
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    year_founded: Mapped[int | None] = mapped_column(SmallInteger)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When this school's own website was last visited for contacts, fee and
    #: student numbers. What makes the enrichment pass resumable: a re-run skips
    #: schools already done rather than starting from the beginning.
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Populated by resolve/build.py, deliberately NOT by a trigger, so a rebuild
    # stays a pure function of its inputs. Uses 'simple', not 'english'.
    search_tsv: Mapped[str | None] = mapped_column(TSVECTOR)


class InstitutionBoard(Base):
    """Many-to-many. A school can hold CBSE and CAIE_IGCSE at once; both rows exist.

    Board filters are EXISTS subqueries, never equality on a column.
    """

    __tablename__ = "institution_boards"

    institution_id: Mapped[int] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE"), primary_key=True
    )
    board: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    # Never guessed. verified_single_owner needs matching legal_entity_norm
    # (ADR-005); coaching_chain needs a first-party directory import (ADR-008);
    # everything else is possible_franchise_network and renders distinctly.
    group_type: Mapped[str] = mapped_column(Text, nullable=False)
    legal_entity_name: Mapped[str | None] = mapped_column(Text)
    legal_entity_norm: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    hq_city: Mapped[str | None] = mapped_column(Text)
    hq_state: Mapped[str | None] = mapped_column(Text)
    campus_count: Mapped[int | None] = mapped_column(Integer)
    # Hard gate + affordability floor. Computed before scoring; see M4-1b.
    qualifying_campus_count: Mapped[int | None] = mapped_column(Integer)
    states_present: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    evidence: Mapped[str] = mapped_column(Text, nullable=False)


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (Index("ix_people_normalized_name", "normalized_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)


class Role(Base):
    """A trust CEO belongs to a group, not a school; principals move between
    institutions. Hence roles rather than people.school_id.
    """

    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint(
            "institution_id IS NOT NULL OR group_id IS NOT NULL",
            name="ck_roles_has_subject",
        ),
        Index("ix_roles_institution_title", "institution_id", "title_normalized"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"), nullable=False)
    institution_id: Mapped[int | None] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE")
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE")
    )
    title_raw: Mapped[str] = mapped_column(Text, nullable=False)
    title_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Score(Base):
    """Versioned rows, not a column on institutions (ADR-010).

    History is retained by design: a 60->90 jump is explained by diffing two
    `components` blobs.
    """

    __tablename__ = "scores"
    __table_args__ = (
        CheckConstraint("fit >= 0 AND fit <= 100", name="ck_scores_fit"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100", name="ck_scores_confidence"
        ),
        Index(
            "ix_scores_model_campaign_fit",
            "model_version",
            "campaign",
            text("fit DESC"),
        ),
    )

    institution_id: Mapped[int] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE"), primary_key=True
    )
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    campaign: Mapped[str] = mapped_column(
        Text, primary_key=True, server_default=text("'default'")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    fit: Mapped[float] = mapped_column(Numeric, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric, nullable=False)
    # Mandatory. One entry per component: raw value, points, max, reason.
    components: Mapped[dict] = mapped_column(JSONB, nullable=False)
    flags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )


class Signal(Base):
    """Deterministic diffs of two authoritative snapshots (ADR-002).

    No severity, no confidence, no review gate - every v1 signal is a registry
    diff. A revived news pipeline would get its own table.
    """

    __tablename__ = "signals"
    __table_args__ = (
        Index(
            "uq_signals_institution_type_date",
            "institution_id",
            "signal_type",
            "event_date",
            unique=True,
            postgresql_where=text("institution_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int | None] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE")
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE")
    )
    signal_type: Mapped[str] = mapped_column(Text, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    event_date: Mapped[date | None] = mapped_column(Date)
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_registry.id"), nullable=False
    )
    evidence_fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetches.id"))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class RegistrySnapshot(Base):
    __tablename__ = "registry_snapshots"
    __table_args__ = (
        Index(
            "ix_registry_snapshots_source_scope",
            "source_id",
            "scope",
            text("taken_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_registry.id"), nullable=False
    )
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        Text, ForeignKey("raw_documents.content_hash"), nullable=False
    )
    #: Which states this snapshot covers. The drift guard and M4-3's diffing
    #: compare only snapshots of the SAME scope - a national run against a
    #: one-state run looks like a 93% collapse and would abort or emit
    #: thousands of false disaffiliation signals.
    scope: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Operational tables
# --------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_not_before_pending",
            "not_before",
            postgresql_where=text("state = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("3")
    )
    not_before: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewQueue(Base):
    __tablename__ = "review_queue"
    __table_args__ = (
        Index(
            "ix_review_queue_kind_score",
            "kind",
            text("score DESC"),
            postgresql_where=text("state = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 'merge_candidate'|'conflict'|'low_confidence_extraction'
    # |'unreadable_fee_document'|'ocr_fee_unverified'  (ADR-016)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'")
    )
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Override(Base):
    """Human corrections. Applied LAST by the resolver, above every source.

    These MUST survive every rebuild - that is the entire point. `reason` is
    mandatory. Two control fields live here rather than as columns because they
    are decisions, not observations: `excluded` and `priority_boost`.
    """

    __tablename__ = "overrides"
    __table_args__ = (
        Index(
            "uq_overrides_live",
            "entity_type",
            "entity_id",
            "field",
            unique=True,
            postgresql_where=text("expires_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Control fields settable through `overrides` that are not canonical columns.
OVERRIDE_CONTROL_FIELDS = frozenset({"excluded", "priority_boost"})


class MergeDecision(Base):
    """Replayed in (entity_key_a, entity_key_b) sort order so repeated rebuilds
    produce identical results. Never iterate these in insertion order.
    """

    __tablename__ = "merge_decisions"
    __table_args__ = (
        UniqueConstraint("entity_key_a", "entity_key_b", name="uq_merge_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_key_a: Mapped[str] = mapped_column(Text, nullable=False)
    entity_key_b: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_in: Mapped[int | None] = mapped_column(Integer)
    rows_out: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[dict | None] = mapped_column(JSONB)


class DenylistLearned(Base):
    """URLs found at extraction time to contain student PII. The fetch-time
    denylist consults this in addition to its static patterns.
    """

    __tablename__ = "denylist_learned"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    added_by: Mapped[str] = mapped_column(Text, nullable=False)


class EvalGold(Base):
    """ADR-015. Hand-verified ground truth. Never machine-written."""

    __tablename__ = "eval_gold"

    institution_key: Mapped[str] = mapped_column(Text, primary_key=True)
    field: Mapped[str] = mapped_column(Text, primary_key=True)
    expected_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    verified_by: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
