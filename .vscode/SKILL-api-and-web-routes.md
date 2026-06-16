# Skill: Adding API Endpoints and Web Routes

**Purpose**: Guide AI agents through adding new API endpoints, web pages, authentication, and form handling in FastAPI and Jinja2.

## Overview

Mileage Logger uses:
- **API routes** (`mileage_logger/api/routes.py`) — JSON responses for OwnTracks integration
- **Web routes** (`mileage_logger/web/routes.py`) — Server-rendered HTML pages via Jinja2
- **Authentication** — Web login (session-based) + OwnTracks API authentication
- **FastAPI** — Modern async web framework with automatic OpenAPI docs

---

## API Routes Structure

### Location

[mileage_logger/api/routes.py](mileage_logger/api/routes.py)

### Basic Pattern

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from mileage_logger.database import get_db

router = APIRouter()

@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@router.post("/trips/{trip_id}")
def update_trip(
    trip_id: int,
    update: TripUpdate,  # Pydantic model from schemas.py
    db: Session = Depends(get_db),
) -> dict[str, str]:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    # ... update logic ...
    return {"status": "updated"}
```

### Key Features

- **Pydantic models** (`schemas.py`) for request/response validation
- **`Depends(get_db)`** to inject database session
- **Automatic documentation** at `/docs` (Swagger UI)
- **HTTPException** for error responses

---

## OwnTracks API Authentication

### Authentication Methods

OwnTracks supports two auth methods, checked in `verify_owntracks_auth()`:

```python
# Method 1: HTTP Basic Auth
# Headers: Authorization: Basic base64(OWNTRACKS_USERNAME:OWNTRACKS_PASSWORD)

# Method 2: API Token
# Headers: X-Api-Key: <OWNTRACKS_API_TOKEN>
# Or:      Authorization: Bearer <OWNTRACKS_API_TOKEN>
```

### Protecting Endpoints

```python
from mileage_logger.api.deps import verify_owntracks_auth

@router.post("/owntracks")
async def owntracks_http(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    verify_owntracks_auth(request)  # Raises HTTPException(401) if auth fails
    # ... process OwnTracks payload ...
```

The `/api/health` endpoint is public; `/api/owntracks` requires authentication.

### Credentials Configuration

```env
# .env
OWNTRACKS_USERNAME=owntracks
OWNTRACKS_PASSWORD=secret-password
OWNTRACKS_API_TOKEN=secret-token
```

---

## Adding a New API Endpoint

### Step 1: Define Request/Response Models

Edit [mileage_logger/schemas.py](mileage_logger/schemas.py):

```python
from pydantic import BaseModel, Field
from datetime import date

class MyRequestModel(BaseModel):
    trip_id: int
    custom_field: str = Field(..., min_length=1, max_length=100)

class MyResponseModel(BaseModel):
    status: str
    trip_id: int
    updated_at: str
```

### Step 2: Add the Route

Edit [mileage_logger/api/routes.py](mileage_logger/api/routes.py):

```python
@router.post("/custom-endpoint")
def my_endpoint(
    request: MyRequestModel,
    db: Session = Depends(get_db),
) -> MyResponseModel:
    logger.info("Processing custom request trip_id=%s", request.trip_id)
    
    trip = db.get(Trip, request.trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    
    # ... process request ...
    
    return MyResponseModel(status="ok", trip_id=trip.id, updated_at=datetime.now().isoformat())
```

### Step 3: Add Authentication (if needed)

For OwnTracks access:
```python
@router.post("/custom-owntracks-endpoint")
def my_endpoint(
    request: Request,
    payload: MyRequestModel,
    db: Session = Depends(get_db),
) -> dict:
    verify_owntracks_auth(request)  # Protect this endpoint
    # ...
```

### Step 4: Test

```bash
# GET (no auth)
curl http://localhost:8000/api/health

# POST with auth (HTTP Basic)
curl -X POST http://localhost:8000/api/custom-endpoint \
  -H "Content-Type: application/json" \
  -u owntracks:password \
  -d '{"trip_id": 1, "custom_field": "value"}'

# API Key auth
curl -X POST http://localhost:8000/api/custom-endpoint \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: secret-token" \
  -d '{"trip_id": 1, "custom_field": "value"}'
```

---

## Web Routes Structure

### Location

[mileage_logger/web/routes.py](mileage_logger/web/routes.py)

### Basic Pattern

```python
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=["web/templates"])

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> TemplateResponse:
    # Protect page with login
    await authenticate_web_credentials(request)
    
    trips = db.scalars(select(Trip).limit(10)).all()
    
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"trips": trips, "title": "Dashboard"},
    )
```

### Jinja2 Template Filters

Custom filters are registered in [web/routes.py](mileage_logger/web/routes.py):

```python
def _format_local_datetime(value, fmt: str = "%Y-%m-%d %I:%M:%S %p") -> str:
    return datetime_to_local(value).strftime(fmt)

templates.env.filters["local_datetime"] = _format_local_datetime

# Use in template: {{ trip.started_at | local_datetime }}
```

---

## Adding a New Web Page

### Step 1: Create Jinja2 Template

Create file: `mileage_logger/web/templates/my_page.html`

```html
{% extends "layout.html" %}

{% block content %}
<div class="container">
    <h1>My Page</h1>
    <ul>
    {% for trip in trips %}
        <li>{{ trip.trip_date }} — {{ trip.origin_display_name }} → {{ trip.destination_display_name }}</li>
    {% endfor %}
    </ul>
</div>
{% endblock %}
```

### Step 2: Add the Route

Edit [mileage_logger/web/routes.py](mileage_logger/web/routes.py):

```python
@router.get("/my-page", response_class=HTMLResponse)
def my_page(
    request: Request,
    db: Session = Depends(get_db),
) -> TemplateResponse:
    # Require login if enabled
    if web_login_enabled():
        authenticate_web_credentials(request)
    
    trips = db.scalars(select(Trip)).all()
    
    return templates.TemplateResponse(
        request,
        "my_page.html",
        {"trips": trips},
    )
```

### Step 3: Add Navigation Link

Edit `mileage_logger/web/templates/layout.html`:

```html
<nav>
    <a href="/">Dashboard</a>
    <a href="/trips">Trips</a>
    <a href="/my-page">My Page</a>  <!-- Add link -->
</nav>
```

---

## Web Login Authentication

### Overview

Session-based authentication with optional IP allowlist:

```env
# Enable login
WEB_LOGIN_USERNAME=admin
WEB_LOGIN_PASSWORD=secret

# Optional: Restrict by IP (CIDR notation)
WEB_ALLOWED_CIDRS=192.168.1.0/24,10.8.0.0/24

# For local testing (disable HTTPS cookie)
WEB_SESSION_COOKIE_SECURE=false
```

### Protecting Routes

```python
from mileage_logger.web.auth import authenticate_web_credentials

@router.get("/protected-page")
def protected_page(request: Request) -> TemplateResponse:
    # Raises HTTPException(403) if not authenticated
    authenticate_web_credentials(request)
    return templates.TemplateResponse(request, "protected.html", {})
```

### Login Flows

1. User visits `/` → redirected to `/login` if not authenticated
2. User enters credentials
3. System checks against `WEB_LOGIN_USERNAME` / `WEB_LOGIN_PASSWORD`
4. On match: Session cookie set, user allowed access
5. Failed attempts: Temporary lockout (`WEB_LOGIN_MAX_ATTEMPTS` x `WEB_LOGIN_LOCKOUT_SECONDS`)

---

## Form Handling

### GET Form Submission

```html
<!-- templates/my_form.html -->
<form method="get" action="/search">
    <input type="text" name="q" placeholder="Search">
    <button type="submit">Search</button>
</form>
```

```python
@router.get("/search")
def search(
    request: Request,
    q: str = Query(...),
    db: Session = Depends(get_db),
) -> TemplateResponse:
    results = db.scalars(
        select(Trip).where(Trip.origin_name.ilike(f"%{q}%"))
    ).all()
    return templates.TemplateResponse(request, "search_results.html", {"results": results})
```

### POST Form Submission

```html
<!-- templates/create_trip.html -->
<form method="post" action="/trips">
    <input type="date" name="trip_date" required>
    <input type="text" name="origin_name" required>
    <input type="text" name="destination_name" required>
    <input type="number" name="miles" step="0.1" required>
    <button type="submit">Add Trip</button>
</form>
```

```python
from fastapi import Form

@router.post("/trips")
async def create_trip(
    request: Request,
    trip_date: date = Form(...),
    origin_name: str = Form(...),
    destination_name: str = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = create_manual_trip(
        db, trip_date, origin_name, destination_name,
        ... # start/end coords
    )
    return RedirectResponse(url="/trips", status_code=303)
```

---

## Database Session Management

### Getting a Session

```python
from mileage_logger.database import get_db

@router.get("/trips")
def list_trips(db: Session = Depends(get_db)):
    trips = db.scalars(select(Trip)).all()
    return trips
```

The `get_db()` dependency automatically:
- Creates a session
- Commits on success
- Rolls back on exception
- Closes connection

### Lazy Loading

SQLAlchemy relationships can lazy-load from templates. To avoid N+1 queries, use `joinedload`:

```python
from sqlalchemy.orm import joinedload

@router.get("/trips")
def list_trips(db: Session = Depends(get_db)):
    trips = db.scalars(
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
    ).unique().all()
    return trips
```

---

## Response Types

### JSON

```python
from fastapi import APIRouter

@router.get("/api/trips")
def get_trips(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": t.id, "miles": str(t.miles)} for t in db.scalars(select(Trip))]
```

### HTML

```python
from fastapi.responses import HTMLResponse

@router.get("/trips", response_class=HTMLResponse)
def trips_page(request: Request, db: Session = Depends(get_db)) -> TemplateResponse:
    return templates.TemplateResponse(request, "trips.html", {})
```

### File Download

```python
from fastapi.responses import FileResponse

@router.get("/export/pdf")
def export_pdf(db: Session = Depends(get_db)) -> Response:
    pdf = generate_monthly_pdf(db, 2026, 6)
    return Response(
        content=pdf.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf.filename}"'},
    )
```

### Redirects

```python
from fastapi.responses import RedirectResponse

@router.post("/login")
async def login(credentials: LoginRequest) -> RedirectResponse:
    # ... verify credentials ...
    return RedirectResponse(url="/dashboard", status_code=303)
```

---

## Logging

Use named loggers for different modules:

```python
import logging

logger = logging.getLogger(__name__)  # Gets "mileage_logger.api.routes"

@router.post("/trips/{trip_id}")
def update_trip(...):
    logger.info("Updated trip trip_id=%s miles=%s", trip_id, update.miles)
    # Shows in logs and `/diagnostics` page
```

---

## Error Handling

### HTTPException

```python
from fastapi import HTTPException

@router.get("/trips/{trip_id}")
def get_trip(trip_id: int, db: Session = Depends(get_db)):
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return trip
```

### Custom Exception Handlers

```python
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )
```

---

## Configuration and Environment

All routes can access settings:

```python
from mileage_logger.config import get_settings

@router.get("/config")
def get_config() -> dict[str, str]:
    settings = get_settings()
    return {
        "vehicle_mpg": str(settings.vehicle_mpg),
        "local_timezone": settings.local_timezone,
    }
```

---

## Testing Routes

### API Tests

```python
from fastapi.testclient import TestClient
from mileage_logger.app import app

client = TestClient(app)

def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_owntracks_requires_auth():
    response = client.post("/api/owntracks", json={})
    assert response.status_code == 401
```

### Web Tests

See [tests/test_web.py](tests/test_web.py) for examples of testing:
- Page rendering
- Form submissions
- Login flows
- Database queries

---

## References

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Jinja2 Template Documentation](https://jinja.palletsprojects.com/)
- [api/routes.py](mileage_logger/api/routes.py) — Existing API endpoints
- [web/routes.py](mileage_logger/web/routes.py) — Existing web routes
- [web/templates/](mileage_logger/web/templates/) — Template examples
- [schemas.py](mileage_logger/schemas.py) — Request/response models
