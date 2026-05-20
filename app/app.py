"""
================================================================================
 app.py -- Flask + Flask-Admin teaching demo for AWS RDS PostgreSQL
================================================================================

 Traffic path:
     Browser  ->  ALB :80  ->  EC2 nginx :80  ->  gunicorn :8000  ->  this app
                                                                       |
                                                                       v
                                                            RDS PostgreSQL :5432

 What the app demonstrates to students:
   1. A Flask web service running on EC2 reads its DB credentials at
      startup from AWS Secrets Manager (no passwords on disk).
   2. SQLAlchemy maps the three tables (customers, drivers, shipments)
      and declares the relationships that exist in the schema.
   3. Flask-Admin auto-generates a CRUD GUI from those mappings, so
      students can click through and *see* the foreign-key relationships
      (one customer -> many shipments, one driver -> many shipments).
   4. A small custom dashboard summarises the data with aggregates so
      students can connect "what the GUI shows" with "what SQL is doing".

 Environment variables (set by systemd in user-data.sh):
   DB_SECRET_NAME   -- name/ARN of the Secrets Manager secret
   AWS_REGION       -- region of the secret (e.g. me-south-1)
   FLASK_SECRET_KEY -- Flask session signing key
================================================================================
"""

import json
import logging
import os

import boto3
from flask import Flask, redirect, render_template_string, url_for
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_admin.menu import MenuLink
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

# --------------------------------------------------------------------------- #
# 1. Pull DB credentials from AWS Secrets Manager                              #
# --------------------------------------------------------------------------- #
# The secret is a JSON blob with this shape:
#   {
#     "username": "appadmin",
#     "password": "<secret>",
#     "engine":   "postgres",
#     "host":     "<rds-endpoint>",
#     "port":     5432,
#     "dbname":   "logistics"
#   }
# The EC2 instance's IAM role grants secretsmanager:GetSecretValue on this one
# secret. boto3 picks the role up automatically from the EC2 metadata service
# -- we never see or store the password ourselves.

SECRET_NAME = os.environ["DB_SECRET_NAME"]
REGION      = os.environ.get("AWS_REGION", "me-south-1")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")


def load_db_credentials() -> dict:
    log.info("Fetching DB credentials from Secrets Manager (%s)", SECRET_NAME)
    sm = boto3.client("secretsmanager", region_name=REGION)
    resp = sm.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(resp["SecretString"])


creds = load_db_credentials()
DB_URI = (
    f"postgresql+psycopg2://{creds['username']}:{creds['password']}"
    f"@{creds['host']}:{creds['port']}/{creds['dbname']}"
)

# --------------------------------------------------------------------------- #
# 2. Flask + SQLAlchemy setup                                                  #
# --------------------------------------------------------------------------- #
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"]        = DB_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"]                     = os.environ.get(
    "FLASK_SECRET_KEY", "change-me-in-prod"
)
# Recycle pooled connections before RDS idle timeout drops them.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle":  280,
}

db = SQLAlchemy(app)

# --------------------------------------------------------------------------- #
# 3. Models -- mirror the schema created by schema.sql                         #
#                                                                              #
# Note: we are NOT using db.create_all() here. The DDL was run from the        #
# bastion. The models just *describe* what already exists so SQLAlchemy can    #
# query it. This keeps schema management out of the running app, which is      #
# safer when you have an Auto Scaling Group with N instances.                  #
# --------------------------------------------------------------------------- #


class Customer(db.Model):
    __tablename__ = "customers"
    customer_id = db.Column(db.BigInteger, primary_key=True)
    full_name   = db.Column(db.String(120), nullable=False)
    email       = db.Column(db.String(255), unique=True, nullable=False)
    phone       = db.Column(db.String(30))
    city        = db.Column(db.String(80))
    created_at  = db.Column(db.DateTime(timezone=True))

    # One customer -> many shipments
    shipments = db.relationship("Shipment", back_populates="customer", lazy="dynamic")

    def __repr__(self):
        # Flask-Admin uses __repr__ (or __str__) to label foreign-key dropdowns
        return f"{self.full_name} <{self.email}>"


class Driver(db.Model):
    __tablename__ = "drivers"
    driver_id      = db.Column(db.BigInteger, primary_key=True)
    full_name      = db.Column(db.String(120), nullable=False)
    license_number = db.Column(db.String(40),  unique=True, nullable=False)
    phone          = db.Column(db.String(30))
    vehicle_plate  = db.Column(db.String(20))
    hired_at       = db.Column(db.Date)

    # One driver -> many shipments
    shipments = db.relationship("Shipment", back_populates="driver", lazy="dynamic")

    def __repr__(self):
        return f"{self.full_name} [{self.vehicle_plate}]"


class Shipment(db.Model):
    __tablename__ = "shipments"
    shipment_id     = db.Column(db.BigInteger, primary_key=True)
    tracking_number = db.Column(db.String(30), unique=True, nullable=False)
    customer_id     = db.Column(db.BigInteger,
                                db.ForeignKey("customers.customer_id"),
                                nullable=False)
    driver_id       = db.Column(db.BigInteger,
                                db.ForeignKey("drivers.driver_id"))
    origin          = db.Column(db.String(120))
    destination     = db.Column(db.String(120))
    weight_kg       = db.Column(db.Numeric(8, 2))
    status          = db.Column(db.String(20), nullable=False)
    created_at      = db.Column(db.DateTime(timezone=True))
    delivered_at    = db.Column(db.DateTime(timezone=True))

    customer = db.relationship("Customer", back_populates="shipments")
    driver   = db.relationship("Driver",   back_populates="shipments")

    def __repr__(self):
        return f"{self.tracking_number} ({self.status})"


# --------------------------------------------------------------------------- #
# 4. Flask-Admin views                                                         #
#                                                                              #
# Each ModelView gives students a full CRUD page. The customisations below     #
# are what make the relationships obvious in the GUI:                          #
#   * column_list lets us show a computed "# Shipments" column.                #
#   * column_filters / column_searchable_list let students slice the data.    #
#   * Foreign-key columns ('customer', 'driver' on the Shipment view) become  #
#     clickable dropdowns thanks to the relationships defined above.          #
# --------------------------------------------------------------------------- #


class CustomerView(ModelView):
    column_list = (
        "customer_id", "full_name", "email", "city", "shipment_count",
    )
    column_searchable_list = ("full_name", "email", "city")
    column_filters         = ("city",)
    column_labels          = {"shipment_count": "# Shipments"}

    # Custom formatter -> count related shipments per row
    def _shipment_count(view, context, model, name):
        return model.shipments.count()

    column_formatters = {"shipment_count": _shipment_count}


class DriverView(ModelView):
    column_list = (
        "driver_id", "full_name", "vehicle_plate",
        "license_number", "shipment_count",
    )
    column_searchable_list = ("full_name", "vehicle_plate", "license_number")
    column_labels          = {"shipment_count": "# Shipments"}

    def _shipment_count(view, context, model, name):
        return model.shipments.count()

    column_formatters = {"shipment_count": _shipment_count}


class ShipmentView(ModelView):
    column_list = (
        "tracking_number", "customer", "driver",
        "origin", "destination", "status", "created_at",
    )
    column_searchable_list = ("tracking_number", "origin", "destination")
    column_filters         = ("status", "origin", "destination")
    column_default_sort    = ("created_at", True)  # newest first
    form_choices = {
        "status": [
            ("pending",    "Pending"),
            ("in_transit", "In Transit"),
            ("delivered",  "Delivered"),
            ("delayed",    "Delayed"),
            ("canceled",   "Canceled"),
        ]
    }


admin = Admin(
    app,
    name="Logistics RDS Demo",
    template_mode="bootstrap4",
    url="/admin",
)
admin.add_view(CustomerView(Customer, db.session, name="Customers"))
admin.add_view(DriverView(Driver,     db.session, name="Drivers"))
admin.add_view(ShipmentView(Shipment, db.session, name="Shipments"))
admin.add_link(MenuLink(name="Dashboard", url="/dashboard"))


# --------------------------------------------------------------------------- #
# 5. Custom dashboard -- aggregate views students can connect to SQL          #
# --------------------------------------------------------------------------- #

DASHBOARD_HTML = """
<!doctype html>
<html><head>
  <title>Logistics RDS Dashboard</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/bootstrap@4.6.2/dist/css/bootstrap.min.css">
  <style>
    body { padding: 24px; font-family: system-ui, sans-serif; }
    .card { margin-bottom: 18px; }
    h1 { margin-bottom: 24px; }
    .num { font-size: 2rem; font-weight: 600; }
  </style>
</head><body>
  <h1>Logistics RDS Dashboard</h1>
  <p><a class="btn btn-primary" href="/admin/">Open Admin GUI &rarr;</a></p>

  <div class="row">
    <div class="col-md-4"><div class="card p-3">
      <div class="text-muted">Customers</div><div class="num">{{ cust }}</div>
    </div></div>
    <div class="col-md-4"><div class="card p-3">
      <div class="text-muted">Drivers</div><div class="num">{{ drv }}</div>
    </div></div>
    <div class="col-md-4"><div class="card p-3">
      <div class="text-muted">Shipments</div><div class="num">{{ shp }}</div>
    </div></div>
  </div>

  <div class="card p-3">
    <h4>Shipments by status</h4>
    <table class="table table-sm">
      <thead><tr><th>Status</th><th>Count</th></tr></thead>
      <tbody>
      {% for status, n in by_status %}
        <tr><td>{{ status }}</td><td>{{ n }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
    <small class="text-muted">SQL:
      <code>SELECT status, COUNT(*) FROM shipments GROUP BY status;</code></small>
  </div>

  <div class="row">
    <div class="col-md-6"><div class="card p-3">
      <h4>Top customers by shipment count</h4>
      <table class="table table-sm">
        <thead><tr><th>Customer</th><th>Shipments</th></tr></thead>
        <tbody>
        {% for name, n in top_customers %}
          <tr><td>{{ name }}</td><td>{{ n }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div></div>
    <div class="col-md-6"><div class="card p-3">
      <h4>Top drivers by shipment count</h4>
      <table class="table table-sm">
        <thead><tr><th>Driver</th><th>Shipments</th></tr></thead>
        <tbody>
        {% for name, n in top_drivers %}
          <tr><td>{{ name }}</td><td>{{ n }}</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div></div>
  </div>

  <p class="text-muted">
    Served by EC2 instance: <code>{{ host }}</code>
  </p>
</body></html>
"""


@app.route("/dashboard")
def dashboard():
    cust = db.session.query(func.count(Customer.customer_id)).scalar()
    drv  = db.session.query(func.count(Driver.driver_id)).scalar()
    shp  = db.session.query(func.count(Shipment.shipment_id)).scalar()

    by_status = (
        db.session.query(Shipment.status, func.count(Shipment.shipment_id))
        .group_by(Shipment.status)
        .order_by(Shipment.status)
        .all()
    )

    # JOIN demos -- exactly what you'd write in raw SQL, expressed in ORM
    top_customers = (
        db.session.query(Customer.full_name,
                         func.count(Shipment.shipment_id))
        .join(Shipment, Shipment.customer_id == Customer.customer_id)
        .group_by(Customer.full_name)
        .order_by(func.count(Shipment.shipment_id).desc())
        .limit(5)
        .all()
    )
    top_drivers = (
        db.session.query(Driver.full_name,
                         func.count(Shipment.shipment_id))
        .join(Shipment, Shipment.driver_id == Driver.driver_id)
        .group_by(Driver.full_name)
        .order_by(func.count(Shipment.shipment_id).desc())
        .limit(5)
        .all()
    )

    return render_template_string(
        DASHBOARD_HTML,
        cust=cust, drv=drv, shp=shp,
        by_status=by_status,
        top_customers=top_customers,
        top_drivers=top_drivers,
        host=os.uname().nodename,
    )


# --------------------------------------------------------------------------- #
# 6. Plumbing                                                                  #
# --------------------------------------------------------------------------- #


@app.route("/")
def root():
    return redirect(url_for("dashboard"))


@app.route("/health")
def health():
    """ALB target-group health check hits this. Returns 200 only if the DB
    is reachable -- so an unhealthy instance is replaced automatically."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return "ok", 200
    except Exception as exc:
        log.warning("health check failed: %s", exc)
        return "db unreachable", 503


if __name__ == "__main__":
    # Local debugging only. In production gunicorn imports `app` directly.
    app.run(host="0.0.0.0", port=8000, debug=False)
