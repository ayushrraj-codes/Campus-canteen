# GEC Khagaria Campus Canteen

The existing page is a campus canteen preorder system. The FastAPI service owns accounts, menu availability, orders, kitchen status, cash-at-pickup records, and the live wait estimate. The browser keeps only the basket and a short-lived bearer token; it does not determine prices or persist shared records.

## Backend Structure

```text
backend/
  app/
    ai/wait_time.py    Queue estimator, isolated from API/database code
    api.py             REST routes and database-backed workflows
    config.py          Environment configuration and validation
    database.py        SQLAlchemy engine and request sessions
    main.py            FastAPI app, CORS, lifecycle, error response
    menu_data.py       Initial canteen catalog
    models.py          SQLAlchemy database tables
    schemas.py         Validated request models
    security.py        Password hashing, JWTs, role dependencies
  tests/               API and estimator tests
  Dockerfile
  requirements.txt
render.yaml            Example Render deployment blueprint
index.html             Existing canteen frontend, connected to the API
```

## API Endpoints

All application routes are prefixed with `/api`. FastAPI's interactive OpenAPI documentation is at `/docs`; the schema is at `/openapi.json`.

| Method | Endpoint | Purpose | Access |
| --- | --- | --- | --- |
| `GET` | `/api/health` | Health check for deployment platforms | Public |
| `GET` | `/api/menu` | Menu, combos, prices, and availability | Public |
| `POST` | `/api/auth/register` | Create a student or faculty account | Public |
| `POST` | `/api/auth/login` | Authenticate and return a bearer token | Public |
| `POST` | `/api/auth/google` | Verify a Google ID token; sign in or create a student/faculty account | Public |
| `GET` | `/api/auth/me` | Validate the current session and return its user | Signed in |
| `PATCH` | `/api/profile/me` | Update your name, mobile number, or profile photo | Signed in |
| `GET` | `/api/predict/wait-time` | Live queue counts, recent prep average, crowd, ETA | Public |
| `POST` | `/api/orders` | Place an order; server checks stock and calculates its total | Student/faculty |
| `GET` | `/api/orders/me` | Return the signed-in customer's order history | Student/faculty |
| `GET` | `/api/payments/me` | Return payments recorded when orders are collected | Student/faculty |
| `POST` | `/api/payments/verify` | Verify Razorpay signature and captured UPI payment before releasing the order to the kitchen | Student/faculty |
| `POST` | `/api/payments/orders/{token}/checkout` | Resume checkout for the customer's pending UPI order | Student/faculty |
| `POST` | `/api/payments/orders/{token}/reference` | Submit a UTR for a direct VPA payment; order remains pending review | Student/faculty |
| `POST` | `/api/admin/orders/{token}/confirm-upi` | Confirm the submitted transfer after checking the merchant account | Admin |
| `POST` | `/api/payments/webhook` | Verify Razorpay webhook signature and reconcile captured UPI payment | Razorpay |
| `GET` | `/api/orders?order_filter=Active` | Kitchen queue and order counts; filters: `Active`, `All`, `Ready`, `Collected` | Admin |
| `PATCH` | `/api/orders/{token}/status` | Move an order one valid step through the kitchen | Admin |
| `PATCH` | `/api/admin/menu/{item_id}/availability` | Update stock visibility and checkout eligibility | Admin |

Errors use JSON `detail` messages with standard HTTP statuses, including `401` for missing/invalid authentication, `403` for the wrong role, `404` for missing records, `409` for conflicts such as unavailable stock, and `422` for invalid input.

## Database

SQLite is used for local development. The deployment blueprint connects the service to PostgreSQL. Tables are created at application startup.

- `users`: unique email, display name, optional mobile number and profile photo, PBKDF2-SHA256 password hash, role, creation time. Uploaded photos are resized to a small JPEG in the browser, validated by MIME signature and size in the API, and stored with the user record; Google profile images may remain HTTPS URLs.
- `menu_items`: catalog identity, category, server-owned integer-rupee price, description, combo flag, and availability.
- `orders`: unique pickup token, customer, pickup slot, status, total, lifecycle timestamps, and measured preparation minutes.
- `order_items`: immutable item name/price snapshot and quantity for each order line.
- `payments`: cash-at-pickup records, pending/manual-review direct UPI references, and pending/paid Razorpay UPI transactions, including provider identifiers, amount, method, status, and receipt identifier.

Roles are enforced by the API. Public registration allows only `student` and `faculty`; the admin account is provisioned from environment variables and cannot be self-selected in signup.

The Snacks category shows cuisine filters for cuisines with active items: Indian, Chinese, Korean, Thai, and South Indian. Its sample listings include aloo samosa (₹25), vegetable spring rolls (₹70), Korean chilli chicken bites (₹110), Thai sweetcorn fritters (₹65), and medu vada (₹45). Cheese Garlic Bread has been retired from the active catalog while historical order records are preserved.

Drinks includes the existing iced strawberry matcha plus mint mojito (non-alcoholic, ₹50), cold coffee (₹70), cafe latte (₹80), chocolate milkshake (₹90), and a virgin fruit cocktail (₹75). Tomato soup (₹60) and sweet corn soup (₹70) are listed under Meals. New dishes include vegetable chow mein (₹85), Hakka noodles (₹90), veg Manchurian (₹100), paneer chilli (₹120), honey chilli potato (₹90), baby corn masala (₹110), masala papad (₹30), chicken pakora (₹110), mushroom chilli (₹110), steamed veg momos (₹80), and chicken roll (₹100). These are suggested starter prices, not confirmed canteen pricing; edit `backend/app/menu_data.py` before launch to match the actual menu.

## Wait-Time Estimator

`backend/app/ai/wait_time.py` is the replaceable model boundary. It takes the number of uncollected orders, currently cooking orders, and up to 30 latest measured preparation times. It computes a recency-weighted average (six minutes until observations exist), derives crowd level from live queue size, and estimates delay from queue load and active cooking capacity. The order's preparation sample is recorded when staff marks it ready. No external AI key or fabricated training data is used; the UI describes this estimate as queue- and history-based.

## Data Flow

1. The page loads catalog and availability from `GET /api/menu`.
2. Signup/login sends credentials to FastAPI. The API hashes/verifies passwords and returns a signed JWT; Google sign-in verifies Google's ID token and verified email. The browser stores the JWT in `sessionStorage` and sends it as `Authorization: Bearer ...`.
3. The basket sends item IDs, quantities, pickup time, and the selected payment method. The API rechecks stock and calculates prices and totals from the database. Cash orders enter the kitchen queue immediately; UPI orders remain outside the kitchen queue until payment is confirmed.
4. If Razorpay keys are configured, Checkout handles UPI and the API verifies the signed, captured transaction directly with Razorpay. Otherwise, checkout generates a VPA payment URI and QR for `UPI_VPA`; the customer submits the UTR from their UPI app, and staff checks the merchant account before confirming. A submitted UTR alone is never treated as proof of payment.
5. Student history and kitchen queue are fetched from the API. Staff status and stock actions use authenticated `PATCH` routes; the API validates role and legal status transitions.
6. On cash order collection, the API records the pay-at-pickup transaction and measured preparation data. The predictor uses the resulting real queue and preparation history.

## Environment Variables

Copy `backend/.env.example` to `backend/.env` and replace the example credentials before running the service.

| Variable | Required | Purpose |
| --- | --- | --- |
| `APP_ENV` | No | Use `development` locally and `production` when deployed. |
| `DATABASE_URL` | No | Defaults to `sqlite:///./canteen.db`; deployment should provide PostgreSQL. |
| `JWT_SECRET_KEY` | Yes in production | Long random signing secret. The app refuses the built-in local-only value in production. |
| `ACCESS_TOKEN_MINUTES` | No | Bearer-token lifetime; defaults to 720 minutes. |
| `ALLOWED_ORIGINS` | Yes when deployed | Comma-separated exact frontend origins, including scheme; no trailing slash. |
| `ADMIN_EMAIL` | Configure for staff | Email of the initial admin account. |
| `ADMIN_PASSWORD` | Configure for staff | Strong initial admin password; set both admin variables together. |
| `ADMIN_NAME` | No | Display name for the provisioned admin. |
| `GOOGLE_CLIENT_ID` | Optional | Google OAuth web client ID. Must match the frontend meta tag. |
| `RAZORPAY_KEY_ID` | Optional | Razorpay test/live public key ID; enables provider checkout. Returned to the browser for Checkout. |
| `RAZORPAY_KEY_SECRET` | Optional | Razorpay API secret; used only by the backend for order creation and payment verification. When both Razorpay keys are configured they take precedence over direct VPA checkout. Never expose it in the frontend. |
| `RAZORPAY_WEBHOOK_SECRET` | Optional | Secret configured for the Razorpay webhook to reconcile provider capture events. |
| `UPI_VPA` | Yes for direct UPI | Merchant VPA for the generated UPI intent and QR. Defaults to `adityaraj123beg@oksbi`. |
| `UPI_PAYEE_NAME` | No | Display name in the UPI payment request; defaults to `GEC Khagaria Canteen`. |

Do not commit `.env` or put secrets in `index.html`. For a deployed frontend, change the `api-base-url` meta tag in `index.html` to the HTTPS API origin (for example `https://canteen-api.example.com`). The frontend origin must also be present in `ALLOWED_ORIGINS`.

To enable Google sign-in, create a Google OAuth 2.0 **Web application** client in Google Cloud Console. Add the local and deployed page origins under **Authorized JavaScript origins** (for example `http://localhost:5500` and your HTTPS frontend domain). Set that same client ID as backend `GOOGLE_CLIENT_ID` and the `google-client-id` meta tag in `index.html`. The API verifies Google's signed ID token and verified email against the configured audience; no client secret belongs in the frontend. Without the client ID, the page explains that Google sign-in is not configured.

Direct UPI uses `UPI_VPA` (already set to the VPA supplied for this site). Customers scan the generated QR or open a compatible UPI app, pay the exact amount, and submit the UTR. Staff must verify receipt in the merchant's UPI/bank app and then use the **UPI payment review** queue to release the order. The application cannot independently validate a UTR against a bank account; staff verification is required for this fallback.

For automated payment confirmation, create a Razorpay account and activate UPI for the merchant account. Start with API keys from Test Mode; put `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` only in `backend/.env`. In Razorpay Dashboard, create a webhook pointing to `https://<your-api-host>/api/payments/webhook`, subscribe to `payment.captured`, and set its secret as `RAZORPAY_WEBHOOK_SECRET`. Switch to Live Mode keys only after merchant onboarding, account verification, webhook configuration, and an end-to-end test. Google Pay appears only when the selected payment app/device supports the UPI intent; the site does not handle or store UPI PINs.

## Local Setup

From PowerShell in the workspace:

```powershell
Set-Location backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and replace JWT_SECRET_KEY, ADMIN_EMAIL, and ADMIN_PASSWORD.
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a second PowerShell window, serve the existing frontend from the workspace root (do not open it as a `file://` page):

```powershell
Set-Location "<workspace-folder>"
python -m http.server 5500
```

Open `http://localhost:5500`. The backend defaults allow this local origin. Sign up as a student/faculty user; sign in as staff using the admin credentials in `.env`. API docs are available at `http://127.0.0.1:8000/docs`.

## Tests

From the workspace root with the backend dependencies installed:

```powershell
python -m pytest backend/tests -q
```

Tests cover menu and stock behavior, registration/login, server-side order pricing, out-of-stock rejection, wait estimation, staff order/payment lifecycle, direct VPA QR generation, UTR submission, staff-only manual confirmation, Razorpay signature verification, and webhook reconciliation.

## Deployment

The included `render.yaml` is a Render Blueprint example: a Dockerized FastAPI service, PostgreSQL database, generated JWT secret, VPA, and health check. Create the Blueprint from the repository, set `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and `ALLOWED_ORIGINS`, then replace the frontend `api-base-url` with the deployed API URL before publishing the static page. Configure Razorpay credentials/webhook only if enabling automatic payment verification. Use the HTTPS frontend origin exactly in `ALLOWED_ORIGINS`. The blueprint uses a persistent PostgreSQL plan; review provider pricing and backup/retention settings for the chosen account before launch.

For another container platform, build from the workspace root with `docker build -f backend/Dockerfile -t gec-canteen-api .`, then supply the same environment values and a reachable PostgreSQL `DATABASE_URL`. Publish port `8000` (or the platform's injected `PORT`); use `/api/health` for the health check. Keep the API and database on private networking where available, use HTTPS at the platform ingress, and rotate the admin/JWT secrets before production.

## System Summary

The frontend remains a static HTML page. FastAPI is the single authority for menu availability, account identity, order totals and status, payments recorded at pickup, and queue estimates. SQLAlchemy isolates persistence, Pydantic validates incoming data, role dependencies protect kitchen actions, and the independent estimator module can be swapped for a trained model later without changing the website's API contract.