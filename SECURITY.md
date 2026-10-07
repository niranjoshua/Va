# Security

## Reporting a problem

Email **tech@vaticore.co.uk** with what you found and how to reproduce it.
Please do not open a public issue for a security problem. We reply within
three working days and tell you what we will do and when.

Please test only against your own local copy (`docker compose up`), never
against vaticore.co.uk services or an operator's data.

## What this repository holds

The forecasting engine, the research notes and the website. It holds no
secrets, keys, operator data or phone numbers: those live only as Render
environment variables and secret files, and `.gitignore` and
`.dockerignore` keep recipient, portfolio and sources files out of git and
out of images.

## How the service protects operators

- Every request needs a key; an operator key reaches only that operator's
  sites (`vaticore/access.py`, `docs/operations.md`).
- Databases accept connections only from Vaticore's own services.
- Meta's webhook is checked against the app secret before anything is read.
- Advisory only: nothing in this code controls an operator's equipment.

## Using this code

The code is published to show the method and to let results be checked. It
is not open source: see `LICENSE`.
