# prcheck.dev

A starter project with a **Django REST** backend and a **React (Vite)** frontend.

## Structure

```
backend/    Django project (core) + api app, DRF, CORS
frontend/   React app scaffolded with Vite
```

## Backend

```bash
cd backend
source venv/bin/activate
python manage.py runserver
```

Runs at http://localhost:8000. Health check: http://localhost:8000/api/health/

To install deps in a fresh clone:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

## Frontend

```bash
cd frontend
npm install
npm run dev
```

Runs at http://localhost:5173. `/api` requests are proxied to the Django server.
