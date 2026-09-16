"""The internal tool. FastAPI + Jinja, no build step (ADR-012).

Every read hits Postgres only. "Get more data" ENQUEUES A JOB and returns
immediately - a click never starts an HTTP fetch (hard rule 3). That is not
bureaucracy: 250 fetches inside a request would time out the browser, ignore
the per-domain rate limits, and lose all its work if the process restarted.
"""

import csv
import io
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from school_intel.api import queries
from school_intel.db import session_scope
from school_intel.jobs import queue

app = FastAPI(title="Tensor School Intelligence", docs_url="/api/docs")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

JUSTDIAL_URL = "https://www.justdial.com/{city}/CBSE-Schools-For-Class-XI/"


def get_session() -> Session:
    with session_scope() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def justdial_link(city: str, cities: list[str]) -> str | None:
    """Only for cities we recognise.

    JustDial silently redirects an unknown city to Mumbai, so offering the link
    for a place we have no schools in would send the sales team to the wrong
    city's listings and look like a bug in our tool.
    """
    slug = city.strip().lower().replace(" ", "-")
    if not slug or city.strip().lower() not in cities:
        return None
    return JUSTDIAL_URL.format(city=slug)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: SessionDep):
    points, boundaries = queries.map_points(session)
    return TEMPLATES.TemplateResponse(
        request,
        "map.html",
        {
            "points": points,
            "boundaries": boundaries,
            "totals": queries.coverage_totals(session),
        },
    )


@app.get("/state/{state}", response_class=HTMLResponse)
def state_view(
    request: Request,
    state: str,
    session: SessionDep,
    contactable: bool = Query(False),
    contact: str = Query("any"),
    class12: str = Query("any"),
):
    if contactable:
        contact = "verified"
    schools = queries.schools_in_state(session, state, contact=contact, class12=class12)
    return TEMPLATES.TemplateResponse(
        request,
        "schools.html",
        {
            "title": state.title(),
            "scope_kind": "state",
            "scope_value": state,
            "schools": schools,
            "contactable": contactable,
            "contact": contact if contact in queries.CONTACT_FILTERS else "any",
            "class12": class12 if class12 in queries.CLASS12_FILTERS else "any",
            "justdial": None,
        },
    )


@app.get("/city", response_class=HTMLResponse)
def city_view(request: Request, session: SessionDep, q: str = Query("")):
    schools = queries.schools_in_city(session, q) if q else []
    return TEMPLATES.TemplateResponse(
        request,
        "schools.html",
        {
            "title": q.title() if q else "Find a city",
            "scope_kind": "city",
            "scope_value": q,
            "schools": schools,
            "contactable": False,
            "justdial": justdial_link(q, queries.known_cities(session)) if q else None,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_view(request: Request, session: SessionDep, q: str = Query("")):
    """Name search. Place words work too - search_tsv carries city/district/state."""
    schools = queries.schools_by_name(session, q) if q else []
    return TEMPLATES.TemplateResponse(
        request,
        "schools.html",
        {
            "title": q if q else "Find a school",
            "scope_kind": "name",
            "scope_value": q,
            "schools": schools,
            "contactable": False,
            "justdial": None,
        },
    )


@app.post("/enrich", response_class=HTMLResponse)
def request_enrichment(
    request: Request,
    session: SessionDep,
    state: Annotated[str, Form()],
    count: Annotated[int, Form()] = 250,
):
    """Queue an enrichment run. Returns instantly; the worker does the fetching.

    dedupe_key means clicking twice does not queue twice - the second click is
    a no-op rather than a doubled crawl of the same schools.
    """
    job_id = queue.enqueue(
        session,
        kind="enrich_state",
        payload={"state": state, "count": count},
        dedupe_key=f"enrich:{state.lower()}:{count}",
    )
    pending = session.execute(
        text(
            "SELECT count(*) FROM jobs"
            " WHERE kind = 'enrich_state' AND state = 'pending'"
        )
    ).scalar()
    return TEMPLATES.TemplateResponse(
        request,
        "queued.html",
        {
            "state": state,
            "count": count,
            "already_queued": job_id is None,
            "pending": pending,
        },
    )


@app.get("/export.csv")
def export_csv(
    session: SessionDep,
    state: str = Query(""),
    city: str = Query(""),
    name: str = Query(""),
    contact: str = Query("any"),
    class12: str = Query("any"),
):
    """The deliverable. Opens in Excel and the sales team can start calling.

    Unknown values are EMPTY CELLS, never 0 - a zero in a spreadsheet gets
    sorted, summed and averaged by whoever opens it, which silently turns
    "we don't know" into "this school has no students".
    """
    if name:
        rows = queries.schools_by_name(session, name, limit=5000)
        label = name
    elif city:
        rows = queries.schools_in_city(session, city, limit=5000)
        label = city
    else:
        rows = queries.schools_in_state(
            session,
            state or "karnataka",
            limit=5000,
            contact=contact,
            class12=class12,
        )
        label = state or "karnataka"

    columns = [
        ("canonical_name", "School"),
        ("principal_name", "Principal"),
        ("phone", "Phone"),
        ("email", "Email"),
        # Header says GUESSED so nobody pastes this column into a mail merge
        # believing the school published it.
        ("email_guessed", "Email (GUESSED - unverified)"),
        ("contact_tier", "Contact tier"),
        ("website", "Website"),
        ("counsellor_name", "Counsellor"),
        ("district", "District"),
        ("state", "State"),
        ("pincode", "PIN"),
        ("address", "Address"),
        ("boards", "Boards"),
        ("fee_annual_inr_mid", "Fee (annual, Rs)"),
        ("fee_year", "Fee year"),
        ("total_enrollment", "Students"),
        # Header says GUESSED - a random DB-wide mean/median draw, not this
        # school's own data. Never mistake it for total_enrollment.
        ("student_count_guessed", "Students (GUESSED - unverified)"),
        ("total_teachers", "Teachers"),
        ("class_12_total", "Class 12"),
        ("year_founded", "Founded"),
        ("legal_entity_name", "Trust / Society"),
        ("fit", "Fit"),
        ("confidence", "Confidence"),
    ]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in columns] + ["Students estimated?", "Notes"])
    for row in rows:
        values = []
        for key, _ in columns:
            value = row.get(key)
            values.append("" if value is None else value)
        values.append("yes" if row.get("enrollment_is_estimated") else "")
        values.append(", ".join(row.get("flags") or []))
        writer.writerow(values)

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="schools-{label.lower()}.csv"'
        },
    )


@app.get("/api/states")
def api_states(session: SessionDep):
    return [
        {
            "state": s.state,
            "total": s.total,
            "senior_secondary": s.senior_secondary,
            "with_phone": s.with_phone,
            "with_email": s.with_email,
            "with_fee": s.with_fee,
            "with_students": s.with_students,
            "enriched": s.enriched,
        }
        for s in queries.state_summary(session)
    ]
