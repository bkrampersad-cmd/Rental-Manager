"""Database models for the Rental Property Manager."""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

# Three roles used only in server mode (standalone mode has no login at all,
# so these never come into play there). Kept as plain strings (not a DB enum)
# so this works identically on SQLite and Postgres.
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLES = [ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER]


class Property(db.Model):
    __tablename__ = "properties"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255))
    city = db.Column(db.String(100))
    state = db.Column(db.String(50))
    zip_code = db.Column(db.String(20))
    purchase_date = db.Column(db.String(20))
    purchase_price = db.Column(db.Float, default=0)
    down_payment = db.Column(db.Float, default=0)
    current_value = db.Column(db.Float, default=0)
    # Tax/depreciation inputs (Schedule E line 18). Land is never
    # depreciable, so the depreciable basis is purchase_price - land_value.
    # placed_in_service_date defaults to purchase_date in the UI but is
    # tracked separately since a property isn't always rent-ready the same
    # day it's bought (e.g. renovated before its first tenant).
    land_value = db.Column(db.Float, default=0)
    placed_in_service_date = db.Column(db.String(20))
    monthly_rent_target = db.Column(db.Float, default=0)
    mortgage_balance = db.Column(db.Float, default=0)
    monthly_mortgage_payment = db.Column(db.Float, default=0)
    units = db.Column(db.Integer, default=1)
    notes = db.Column(db.Text)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship(
        "Transaction", backref="property", lazy=True, cascade="all, delete-orphan"
    )
    bank_accounts = db.relationship(
        "PropertyAccount", backref="property", lazy=True, cascade="all, delete-orphan",
        order_by="PropertyAccount.id",
    )
    # Named after the relationship, not the `units` integer count column
    # above, to avoid a naming clash — that column is just a headline
    # number used in cap-rate math; these are the actual named units (e.g.
    # "Unit A", "2B") transactions can optionally be tagged with.
    property_units = db.relationship(
        "PropertyUnit", backref="property", lazy=True, cascade="all, delete-orphan",
        order_by="PropertyUnit.id",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "purchase_date": self.purchase_date,
            "purchase_price": self.purchase_price,
            "down_payment": self.down_payment,
            "current_value": self.current_value,
            "land_value": self.land_value,
            "placed_in_service_date": self.placed_in_service_date,
            "monthly_rent_target": self.monthly_rent_target,
            "mortgage_balance": self.mortgage_balance,
            "monthly_mortgage_payment": self.monthly_mortgage_payment,
            "units": self.units,
            "notes": self.notes,
            "archived": self.archived,
            "bank_accounts": [a.to_dict() for a in self.bank_accounts],
            "unit_list": [u.to_dict() for u in self.property_units],
        }


ACCOUNT_TYPE_CHECKING = "checking"
ACCOUNT_TYPE_SAVINGS = "savings"
ACCOUNT_TYPE_OTHER = "other"
ACCOUNT_TYPES = [ACCOUNT_TYPE_CHECKING, ACCOUNT_TYPE_SAVINGS, ACCOUNT_TYPE_OTHER]


class PropertyAccount(db.Model):
    """A bank account associated with a property (e.g. the checking account
    rent gets deposited into). Used to auto-fill/suggest the Account field
    on transactions for that property, and to label transactions clearly
    when several properties' activity gets bundled into one statement."""
    __tablename__ = "property_accounts"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    account_type = db.Column(db.String(20), nullable=False, default=ACCOUNT_TYPE_CHECKING)
    account_number = db.Column(db.String(120))  # last 4 digits or full number, user's choice
    nickname = db.Column(db.String(120))
    # The balance in this account before any transactions in this app were
    # recorded — e.g. what it held the day you started tracking it here.
    # Running balance for reconciliation = starting_balance + everything
    # tagged with this account's label after that.
    starting_balance = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def label(self):
        """Human-readable label used to prefill the Account field, e.g.
        "Checking ...1234" or a custom nickname if one was given."""
        if self.nickname:
            return self.nickname
        type_label = self.account_type.title()
        if self.account_number:
            tail = self.account_number[-4:]
            return f"{type_label} ...{tail}"
        return type_label

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "account_type": self.account_type,
            "account_number": self.account_number,
            "nickname": self.nickname,
            "starting_balance": self.starting_balance,
            "label": self.label(),
        }


class PropertyUnit(db.Model):
    """A named unit within a property (e.g. "Unit A", "2B", "Front House"),
    for multi-unit properties where income/expenses are sometimes worth
    tracking per-unit rather than just at the property level. Separate
    from Property.units (a plain headline count used in cap-rate math) —
    these are the actual named units transactions can be tagged with."""
    __tablename__ = "property_units"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {"id": self.id, "property_id": self.property_id, "name": self.name}


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'income' or 'expense'
    is_default = db.Column(db.Boolean, default=False)
    # Which IRS Schedule E expense line this category's transactions roll up
    # into for the Tax Documents report (see SCHEDULE_E_LINES below). Only
    # meaningful for expense categories — income always rolls up to "Rents
    # Received" regardless of category, the way Schedule E itself works.
    # None means "not yet mapped" and shows up in a review bucket on the
    # report rather than being silently dropped. The two special values
    # "mortgage_principal" and "capital_improvement" are deliberately NOT
    # Schedule E lines (principal isn't deductible; improvements must be
    # capitalized/depreciated, not expensed) — they're tracked informationally
    # instead of being flagged as unmapped.
    schedule_e_line = db.Column(db.String(30))

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "is_default": self.is_default,
            "schedule_e_line": self.schedule_e_line,
        }


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("property_units.id"), nullable=True)
    date = db.Column(db.String(20), nullable=False)  # ISO yyyy-mm-dd
    type = db.Column(db.String(10), nullable=False)  # 'income' or 'expense'
    payee = db.Column(db.String(255))
    amount = db.Column(db.Float, nullable=False)  # always stored positive
    account = db.Column(db.String(120))
    notes = db.Column(db.Text)
    source = db.Column(db.String(20), default="manual")  # manual | import
    import_hash = db.Column(db.String(64), index=True)  # for de-duping imports
    # Filename of an optional attached receipt (image or PDF), stored on disk
    # under DATA_DIR/receipts/ — see app.py's receipt upload/view routes.
    # Null when no receipt has been attached.
    receipt_filename = db.Column(db.String(255))
    # True only for the correcting transaction created from an account
    # reconciliation's discrepancy (see AccountReconciliation below) — flagged
    # so the transaction list can visually set it apart from real income/
    # expense entries, and so the Tax Documents report can exclude it from
    # Schedule E totals rather than silently treating a balance correction
    # as real rental income or a real deductible expense.
    is_adjustment = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Only ever populated in server mode (whoever was logged in when the row
    # was created). Null in standalone mode and for pre-existing rows.
    created_by = db.Column(db.String(80))

    category = db.relationship("Category")
    unit = db.relationship("PropertyUnit")

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else "Uncategorized",
            "unit_id": self.unit_id,
            "unit_name": self.unit.name if self.unit else None,
            "date": self.date,
            "type": self.type,
            "payee": self.payee,
            "amount": self.amount,
            "account": self.account,
            "notes": self.notes,
            "source": self.source,
            "created_by": self.created_by,
            "receipt_filename": self.receipt_filename,
            "is_adjustment": self.is_adjustment,
        }


class AccountReconciliation(db.Model):
    """A snapshot of comparing this app's running balance for a bank
    account against what the bank statement actually shows, as of a given
    date. Created via the "Reconcile" action on a PropertyAccount. Doesn't
    change any transaction data by itself — the user has to explicitly turn
    a discrepancy into a correcting transaction (see
    adjustment_transaction_id), so nothing gets silently altered."""
    __tablename__ = "account_reconciliations"

    id = db.Column(db.Integer, primary_key=True)
    property_account_id = db.Column(db.Integer, db.ForeignKey("property_accounts.id"), nullable=False)
    reconcile_date = db.Column(db.String(20), nullable=False)  # ISO yyyy-mm-dd
    statement_balance = db.Column(db.Float, nullable=False)  # what the bank says
    computed_balance = db.Column(db.Float, nullable=False)   # starting_balance + this app's transactions, as of that date
    discrepancy = db.Column(db.Float, nullable=False)         # statement_balance - computed_balance
    adjustment_transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    account = db.relationship(
        "PropertyAccount", backref=db.backref(
            "reconciliations", lazy=True, cascade="all, delete-orphan",
            order_by="desc(AccountReconciliation.reconcile_date)",
        ),
    )
    adjustment_transaction = db.relationship("Transaction")

    def to_dict(self):
        return {
            "id": self.id,
            "property_account_id": self.property_account_id,
            "reconcile_date": self.reconcile_date,
            "statement_balance": self.statement_balance,
            "computed_balance": self.computed_balance,
            "discrepancy": self.discrepancy,
            "adjustment_transaction_id": self.adjustment_transaction_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


RECURRING_FREQUENCIES = ["weekly", "monthly", "yearly"]


class RecurringTransaction(db.Model):
    """A template for a transaction that repeats on a schedule (rent income,
    a fixed mortgage/insurance payment, etc.) so it doesn't have to be
    re-entered by hand every period. Doesn't touch the Transaction table
    directly — see _generate_due_recurring() in app.py, which lazily creates
    real Transaction rows for every occurrence up through today, the same
    "check when something is looked at" pattern used for auto-seeding a
    property's default units."""
    __tablename__ = "recurring_transactions"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True)
    unit_id = db.Column(db.Integer, db.ForeignKey("property_units.id"), nullable=True)
    type = db.Column(db.String(10), nullable=False)  # 'income' or 'expense'
    payee = db.Column(db.String(255))
    amount = db.Column(db.Float, nullable=False)
    account = db.Column(db.String(120))
    notes = db.Column(db.Text)
    frequency = db.Column(db.String(20), nullable=False, default="monthly")
    start_date = db.Column(db.String(20), nullable=False)
    end_date = db.Column(db.String(20))  # null = repeats indefinitely
    # The next occurrence still owed a generated Transaction row. Advanced
    # forward (by `frequency`) each time one gets generated.
    next_due_date = db.Column(db.String(20), nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    property = db.relationship("Property", backref=db.backref(
        "recurring_transactions", lazy=True, cascade="all, delete-orphan",
    ))
    category = db.relationship("Category")
    unit = db.relationship("PropertyUnit")

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else "Uncategorized",
            "unit_id": self.unit_id,
            "unit_name": self.unit.name if self.unit else None,
            "type": self.type,
            "payee": self.payee,
            "amount": self.amount,
            "account": self.account,
            "notes": self.notes,
            "frequency": self.frequency,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "next_due_date": self.next_due_date,
            "active": self.active,
        }


class ImportRule(db.Model):
    """A simple keyword-matching rule applied during import: if a row's
    payee/description contains `match_text` (case-insensitive), suggest
    `category_id` for it — but only when the import itself didn't already
    resolve a category some other way (an explicitly-mapped Category column
    always wins). Purely a time-saver; nothing here is ever applied to
    transactions entered by hand."""
    __tablename__ = "import_rules"

    id = db.Column(db.Integer, primary_key=True)
    match_text = db.Column(db.String(255), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship("Category")

    def to_dict(self):
        return {
            "id": self.id,
            "match_text": self.match_text,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else None,
        }


class MileageLog(db.Model):
    """A single business-mileage trip logged against a property, for the
    standard-mileage-rate deduction (Schedule E line 6, Auto and Travel).
    `rate_used` is snapshotted at the time the trip is logged rather than
    looked up live from Settings, since the IRS rate itself changes (and has,
    more than once within the same tax year) — so a trip logged in January
    keeps January's rate even if the rate is updated later in the year."""
    __tablename__ = "mileage_logs"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    # Optional — a trip can be tied to one specific unit (e.g. driving out to
    # fix a leak in Unit B) so it lines up with the same per-unit breakout
    # already available for Transactions/Tenants/Recurring rules. Not
    # required: most trips (a mortgage-related errand, a property-wide
    # inspection) reasonably apply to the property as a whole instead.
    unit_id = db.Column(db.Integer, db.ForeignKey("property_units.id"), nullable=True)
    date = db.Column(db.String(20), nullable=False)
    purpose = db.Column(db.String(255))
    miles = db.Column(db.Float, nullable=False)
    rate_used = db.Column(db.Float, nullable=False)
    # Set when this trip was logged via the Distance option on a Transaction
    # (Add/Edit Transaction > an Auto/Travel category > "Distance" instead of
    # "Amount") rather than logged standalone on the Mileage tab. A linked
    # trip's dollar amount already flows into the tax report's Auto and
    # Travel line through its Transaction's category, so the report must
    # only add the *unlinked* trips' deduction on top — otherwise the same
    # dollars would be counted twice. See tax_report.py.
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    property = db.relationship("Property", backref=db.backref(
        "mileage_logs", lazy=True, cascade="all, delete-orphan",
    ))
    unit = db.relationship("PropertyUnit")
    transaction = db.relationship("Transaction")

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "unit_name": self.unit.name if self.unit else None,
            "date": self.date,
            "purpose": self.purpose,
            "miles": self.miles,
            "rate_used": self.rate_used,
            "deduction": round(self.miles * self.rate_used, 2),
            "transaction_id": self.transaction_id,
        }


class Tenant(db.Model):
    """Basic tenant/lease info per property (and optionally per unit) —
    enough to see who's where and when a lease is up, not a full tenant
    portal (no payments/messaging/screening)."""
    __tablename__ = "tenants"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    unit_id = db.Column(db.Integer, db.ForeignKey("property_units.id"), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    lease_start = db.Column(db.String(20))
    lease_end = db.Column(db.String(20))
    monthly_rent = db.Column(db.Float, default=0)
    security_deposit = db.Column(db.Float, default=0)
    active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    property = db.relationship("Property", backref=db.backref(
        "tenants", lazy=True, cascade="all, delete-orphan", order_by="Tenant.id",
    ))
    unit = db.relationship("PropertyUnit")

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "unit_id": self.unit_id,
            "unit_name": self.unit.name if self.unit else None,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "lease_start": self.lease_start,
            "lease_end": self.lease_end,
            "monthly_rent": self.monthly_rent,
            "security_deposit": self.security_deposit,
            "active": self.active,
            "notes": self.notes,
        }


DOCUMENT_TYPES = ["lease", "insurance", "inspection", "tax", "other"]


class PropertyDocument(db.Model):
    """A general file attached to a property (lease, insurance policy,
    inspection report, etc.) — same on-disk storage pattern as transaction
    receipts (see app.py's DATA_DIR/documents/), just not tied to a single
    transaction. `expiration_date` is optional and only meaningful for
    things like insurance policies; when set, it feeds the dashboard's
    "expiring soon" alert."""
    __tablename__ = "property_documents"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255))
    doc_type = db.Column(db.String(20), nullable=False, default="other")
    expiration_date = db.Column(db.String(20))
    notes = db.Column(db.Text)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    property = db.relationship("Property", backref=db.backref(
        "documents", lazy=True, cascade="all, delete-orphan", order_by="desc(PropertyDocument.uploaded_at)",
    ))

    def to_dict(self):
        return {
            "id": self.id,
            "property_id": self.property_id,
            "filename": self.filename,
            "original_filename": self.original_filename,
            "doc_type": self.doc_type,
            "expiration_date": self.expiration_date,
            "notes": self.notes,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text)


class User(db.Model, UserMixin):
    """Login accounts — only ever created/used in server mode. Standalone
    installs never have rows in this table and never see a login screen."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_EDITOR)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Flask-Login's UserMixin supplies is_authenticated/is_anonymous/get_id;
    # we override is_active so a deactivated account is instantly logged out.
    @property
    def is_active(self):
        return bool(self.active)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BackupLog(db.Model):
    """Records every successful backup export taken through the app (Full or
    Business-Profile-Only), so the Danger Zone's reset buttons can require a
    recent backup before allowing anything destructive — this only ever
    logs backups made via the app's own Export buttons; it can't know about
    a copy you made of the file yourself outside the app."""
    __tablename__ = "backup_logs"

    id = db.Column(db.Integer, primary_key=True)
    backup_type = db.Column(db.String(20), nullable=False)  # "full" or "profile"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(80))  # username in server mode, None in standalone

    def to_dict(self):
        return {
            "id": self.id,
            "backup_type": self.backup_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
        }


class ImportSession(db.Model):
    """Holds a CSV import's parsed rows between the preview and commit steps.

    Backed by the database (rather than an in-memory dict) so this works
    correctly whether the app is a single standalone process or a server
    handling requests across multiple worker processes/threads. Sessions
    are self-expiring — anything left over from an abandoned import gets
    cleaned up the next time someone starts a new one.
    """
    __tablename__ = "import_sessions"

    token = db.Column(db.String(64), primary_key=True)
    rows_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


DEFAULT_CATEGORIES = [
    # Income - modeled after Quicken Rental Manager / DoorLoop charts of accounts
    ("Rent Income", "income"),
    ("Late Fees", "income"),
    ("Application Fees", "income"),
    ("Pet Fees / Pet Rent", "income"),
    ("Parking Fees", "income"),
    ("Laundry Income", "income"),
    ("Other Income", "income"),
    # Expenses
    ("Mortgage Interest", "expense"),
    ("Mortgage Principal", "expense"),
    ("Property Tax", "expense"),
    ("Insurance", "expense"),
    ("HOA Fees", "expense"),
    ("Repairs & Maintenance", "expense"),
    ("Capital Improvements", "expense"),
    ("Utilities", "expense"),
    ("Property Management Fees", "expense"),
    ("Landscaping / Snow Removal", "expense"),
    ("Pest Control", "expense"),
    ("Cleaning & Turnover", "expense"),
    ("Legal & Professional Fees", "expense"),
    ("Advertising & Marketing", "expense"),
    ("Supplies", "expense"),
    ("Travel / Mileage", "expense"),
    ("Bank & Processing Fees", "expense"),
    ("Licenses & Permits", "expense"),
    ("Other Expense", "expense"),
]

# IRS Schedule E (Form 1040) expense lines 5-19. "depreciation" (line 18) is
# never assigned to a category — it's computed from each property's
# land_value/placed_in_service_date via depreciation.py, not from tagged
# transactions, since depreciation isn't something you write a check for.
SCHEDULE_E_LINES = [
    ("advertising", 5, "Advertising"),
    ("auto_travel", 6, "Auto and Travel"),
    ("cleaning_maintenance", 7, "Cleaning and Maintenance"),
    ("commissions", 8, "Commissions"),
    ("insurance", 9, "Insurance"),
    ("legal_professional", 10, "Legal and Other Professional Fees"),
    ("management_fees", 11, "Management Fees"),
    ("mortgage_interest", 12, "Mortgage Interest Paid to Banks, Etc."),
    ("other_interest", 13, "Other Interest"),
    ("repairs", 14, "Repairs"),
    ("supplies", 15, "Supplies"),
    ("taxes", 16, "Taxes"),
    ("utilities", 17, "Utilities"),
    ("other", 19, "Other"),
]

# Special values that are valid picks for schedule_e_line but are NOT one of
# the 15 report lines above — they're tracked and shown informationally on
# the tax report instead, since lumping them in as "unmapped" would be
# misleading (they're deliberately excluded from Schedule E, not forgotten).
SCHEDULE_E_NOT_DEDUCTIBLE = "mortgage_principal"   # principal isn't deductible, only interest is
SCHEDULE_E_CAPITALIZE = "capital_improvement"       # must be depreciated, not expensed in-year

# Best-effort default mapping for the built-in category list, so a fresh
# install's Tax Documents report is useful immediately with zero setup —
# users can always re-map any category (built-in or custom) afterward.
DEFAULT_CATEGORY_SCHEDULE_E_MAP = {
    "Mortgage Interest": "mortgage_interest",
    "Mortgage Principal": SCHEDULE_E_NOT_DEDUCTIBLE,
    "Property Tax": "taxes",
    "Insurance": "insurance",
    "HOA Fees": "other",
    "Repairs & Maintenance": "repairs",
    "Capital Improvements": SCHEDULE_E_CAPITALIZE,
    "Utilities": "utilities",
    "Property Management Fees": "management_fees",
    "Landscaping / Snow Removal": "cleaning_maintenance",
    "Pest Control": "cleaning_maintenance",
    "Cleaning & Turnover": "cleaning_maintenance",
    "Legal & Professional Fees": "legal_professional",
    "Advertising & Marketing": "advertising",
    "Supplies": "supplies",
    "Travel / Mileage": "auto_travel",
    "Bank & Processing Fees": "other",
    "Licenses & Permits": "taxes",
    "Other Expense": "other",
}


def seed_categories():
    if Category.query.count() == 0:
        for name, ctype in DEFAULT_CATEGORIES:
            db.session.add(Category(
                name=name, type=ctype, is_default=True,
                schedule_e_line=DEFAULT_CATEGORY_SCHEDULE_E_MAP.get(name),
            ))
        db.session.commit()


def restore_default_categories():
    """Re-adds any of the built-in categories that have been deleted, one
    by one, matched by exact name — unlike seed_categories() (which only
    ever runs once, on a completely empty table), this is safe to call any
    time: it never touches a category that already exists (built-in or
    custom) and never creates a duplicate of one you still have. Returns
    the list of category names that were added back."""
    existing_names = {c.name for c in Category.query.all()}
    added = []
    for name, ctype in DEFAULT_CATEGORIES:
        if name in existing_names:
            continue
        db.session.add(Category(
            name=name, type=ctype, is_default=True,
            schedule_e_line=DEFAULT_CATEGORY_SCHEDULE_E_MAP.get(name),
        ))
        added.append(name)
    if added:
        db.session.commit()
    return added


DEFAULT_SETTINGS = {
    "income_color": "#1a9c5b",
    "expense_color": "#d9463f",
    "accent_color": "#2563eb",
    "currency_symbol": "$",
    # Business profile — shown in the app header and on exported reports so
    # the app and its printouts look like a business's own custom software.
    "business_name": "",
    "business_address": "",
    "business_city": "",
    "business_state": "",
    "business_zip": "",
    "business_phone": "",
    "business_email": "",
    "business_website": "",
    "business_tax_id": "",
    "report_footer": "",
    "fiscal_year_start_month": "1",
    "has_logo": "0",
    # IRS standard mileage rate (Schedule E, Auto and Travel), in dollars per
    # mile. This changes over time — and has, more than once within the same
    # tax year — so it's a setting you're expected to keep current rather
    # than something the app assumes forever. Each logged trip snapshots
    # whatever this was set to at the time (see MileageLog.rate_used), so
    # updating it never rewrites past trips. Defaulting to the rate in effect
    # as of mid-2026 ($0.76/mile, up from $0.725 effective July 1, 2026) —
    # verify against the current IRS-published rate before relying on it.
    "standard_mileage_rate": "0.76",
    # 1099-NEC/MISC federal reporting threshold, in dollars — used only to
    # flag vendors who may need a 1099 on the Tax Documents report, not to
    # file anything automatically. Raised from $600 to $2,000 starting with
    # payments made in 2026 (One Big Beautiful Bill Act); adjusts for
    # inflation from 2027 onward, so this is deliberately editable rather
    # than hardcoded.
    "form_1099_threshold": "2000",
    # Standalone Access Password: how many consecutive wrong guesses (of
    # either the password itself or a security-question answer) are allowed
    # before the app locks out further attempts for a cooldown period. Blank
    # means the feature is off — this is optional, not everyone wants it.
    # (The actual attempt counter and lockout-until timestamp are separate,
    # internal Setting rows — see SENSITIVE_SETTING_KEYS in app.py — not
    # part of this default map, same as access_password_hash itself.)
    "access_password_lockout_attempts": "",
    # Scheduled automatic backups — a Full Backup .zip written straight to a
    # "Backup" folder next to the app (see BACKUP_DIR in app.py), on top of
    # (not instead of) the manual Export/Import buttons. Blank frequency
    # means the feature is off. Retention is capped at 30 files — see
    # _clamp_backup_retention() in app.py — oldest file is deleted first
    # once that cap is hit, never unlimited.
    "backup_auto_frequency": "",       # "" (off) | "weekly" | "30days"
    "backup_auto_retention": "10",     # how many auto-backup .zip files to keep, 1-30
}

# Historical + current IRS standard business mileage rates, (effective_date,
# rate) pairs sorted ascending. There's no public IRS API for this — the IRS
# only publishes rates via press release/notice — so this is a small table
# maintained by hand as new rates are announced, rather than a live lookup.
# Powers the "Update" button next to the mileage rate Setting: it finds the
# latest entry whose effective_date is on or before today and fills that in,
# instead of requiring the user to know/type the current rate themselves.
# This table only affects what the Update button suggests — it never
# rewrites a MileageLog row that's already been logged (see rate_used).
STANDARD_MILEAGE_RATE_HISTORY = [
    ("2023-01-01", 0.655),
    ("2024-01-01", 0.67),
    ("2025-01-01", 0.70),
    ("2026-01-01", 0.725),
    ("2026-07-01", 0.76),
]


def current_standard_mileage_rate(as_of_iso):
    """Returns (rate, effective_date) for the entry in
    STANDARD_MILEAGE_RATE_HISTORY effective on or before as_of_iso (an ISO
    yyyy-mm-dd string). Falls back to the earliest known entry if as_of_iso
    predates the whole table, and to the latest entry if the table is
    somehow empty of anything on/before that date's a no-op safeguard."""
    applicable = [row for row in STANDARD_MILEAGE_RATE_HISTORY if row[0] <= as_of_iso]
    if applicable:
        return applicable[-1]
    return STANDARD_MILEAGE_RATE_HISTORY[0]


def seed_settings():
    for key, value in DEFAULT_SETTINGS.items():
        if not Setting.query.get(key):
            db.session.add(Setting(key=key, value=value))
    db.session.commit()
