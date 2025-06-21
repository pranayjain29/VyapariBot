from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime, timedelta, timezone, date
from supabase import create_client, Client
import csv, tempfile, os
from agents import Agent, Runner, trace, function_tool
from typing import List, Dict, Any

# Initialize Supabase client
url: str = os.environ.get("SUPABASE_URL_KEY")
key: str = os.environ.get("SUPABASE_API_KEY")
supabase: Client = create_client(url, key)

print("Supabase client created")


MAX_HISTORY = 5          # keep only the last 5 messages

# ────────────────────────────────────────────────────────────────────────────────
# 1. Store message and trim history
# ────────────────────────────────────────────────────────────────────────────────
def log_message(chat_id: int, text: str, message_date_utc: int | None) -> None:
    """
    1️.  Insert the incoming message only if we don't already have a row with the
        same (chat_id, message_date).
    2️.  Keep only the newest MAX_HISTORY rows per chat_id.
    """

    # ── Build the message_date value we will store ──────────────────────────
    date_obj = (
        datetime.fromtimestamp(message_date_utc, tz=timezone.utc)
        if message_date_utc
        else datetime.now(timezone.utc)
    )

    msg_iso = date_obj.isoformat()           # identical formatting everywhere

    # ── 1.  Duplicate check  ───────────────────────────────────────────────
    dup_resp = (
        supabase
        .table("vyapari_message_log")
        .select("id")
        .eq("chat_id", str(chat_id))
        .eq("message_date", msg_iso)
        .limit(1)
        .execute()
    )

    if dup_resp.data:                       # duplicate found → skip insert
        return

    """
    1. Inserts the incoming message.
    2. Deletes older rows so that only the newest MAX_HISTORY remain per chat_id.
    """
    

    # ── 2. Insert
    payload = {
        "chat_id":      str(chat_id),
        "message_text": text,
        "message_date": date_obj.isoformat(),
        "inserted_at":  datetime.now(timezone.utc).isoformat()
    }
    supabase.table("vyapari_message_log").insert(payload).execute()

    # ── 3. Trim (delete everything beyond the newest MAX_HISTORY rows)
    old_rows_resp = (
        supabase
        .table("vyapari_message_log")
        .select("id")                       # we only need primary key
        .eq("chat_id", str(chat_id))
        .order("message_date", desc=True)   # newest → oldest
        .offset(MAX_HISTORY)                # skip the first N newest rows
        .execute()
    )

    old_ids = [row["id"] for row in (old_rows_resp.data or [])]
    if old_ids:
        supabase.table("vyapari_message_log").delete().in_("id", old_ids).execute()


# ────────────────────────────────────────────────────────────────────────────────
# 2. Fetch last N (<=5) messages
# ────────────────────────────────────────────────────────────────────────────────
def get_last_messages(chat_id: int, n: int = MAX_HISTORY) -> List[Dict[str, Any]]:
    """
    Returns up to `n` latest messages (oldest → newest).
    """
    resp = (
        supabase
        .table("vyapari_message_log")
        .select("message_text, message_date")
        .eq("chat_id", str(chat_id))
        .order("message_date", desc=True)
        .limit(n)
        .execute()
    )
    data = resp.data or []
    return list(reversed(data))  # chronological order

# ---------------------------------------------------------------------------
# WRITE: insert / update a single user record in table `vyapari_user`
# ---------------------------------------------------------------------------
def write_user(chat_id: int, user_name: str):
    """
    Creates (or updates) a user row in `vyapari_user`.

    Business rules implemented:
        • chat_id is stored as string in DB
        • Default values:
            - registered_on / last_updated : current timestamp (UTC)
            - subscription_tier            : 'Free'
            - plan_start                   : today's date
            - plan_end                     : 31-Jan-2026
            - total_transactions           : 0
            - total_revenue                : 0
    """
    try:
        now_ts = datetime.utcnow().isoformat(timespec="seconds")
        payload = {
            "chat_id":           str(chat_id),
            "registered_on":     now_ts,
            "last_updated":      now_ts,
            "user_name":         user_name,
            "subscription_tier": "Free",
            "plan_start":        date.today().isoformat(),
            "plan_end":          date(2026, 1, 31).isoformat(),   # fixed for all users
            "total_transactions": 0,
            "total_revenue":      0,
        }

        # upsert → insert if new, overwrite (or merge) if chat_id already exists
        response = (
            supabase
            .table('vyapari_user')
            .upsert(payload, on_conflict='chat_id')
            .execute()
        )
        return response.data
    except Exception as e:
        print(f"Error writing user: {e}")
        return None


# ---------------------------------------------------------------------------
# READ: fetch a single user record by chat_id
# ---------------------------------------------------------------------------
def read_user(chat_id: int):
    """Returns user details for the given chat_id from `vyapari_user`."""
    try:
        response = (
            supabase
            .table('vyapari_user')
            .select('*')
            .eq('chat_id', str(chat_id))
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error reading user: {e}")
        return None
# ---------------------------------------------------------------------------
# Helper: update the `last_updated` timestamp (and optionally user_name)
# ---------------------------------------------------------------------------
def update_last_used_date(chat_id: int, user_name: str | None = None) -> None:
    """
    Refreshes `last_updated` for the given chat_id in `vyapari_user`.
    Optionally updates user_name if a new value is supplied.
    """
    try:
        payload = {"last_updated": datetime.utcnow().isoformat(timespec="seconds")}
        if user_name:
            payload["user_name"] = user_name

        supabase.table("vyapari_user").update(payload).eq("chat_id", str(chat_id)).execute()
    except Exception as e:
        print(f"Error updating last_used_date: {e}")

def update_user_data(chat_id: int, transaction_amount: float):
    """
    Increments total_transactions, adds `transaction_amount` to total_revenue,
    and refreshes last_updated for the given user.

    • Creates the user on-the-fly (via write_user) if they don’t already exist.
    • Returns the Supabase response from the update / upsert call.
    """
    try:
        # 1. Fetch existing user (None if not found)
        user_record = read_user(chat_id)

        # 2. If user doesn’t exist, create with defaults first
        if not user_record:
            # We don’t know the user’s name here; pass empty string
            write_user(chat_id, user_name="")
            user_record = read_user(chat_id) or {}

        # 3. Derive new aggregated values
        new_txn_count = (user_record.get("total_transactions") or 0) + 1
        new_revenue   = float(user_record.get("total_revenue") or 0) + float(transaction_amount)

        # 4. Build update payload
        payload = {
            "total_transactions": new_txn_count,
            "total_revenue":      new_revenue,
            "last_updated":       datetime.utcnow().isoformat(timespec="seconds")
        }

        # 5. Persist changes
        response = (
            supabase
            .table("vyapari_user")
            .update(payload)
            .eq("chat_id", str(chat_id))
            .execute()
        )

        return response.data

    except Exception as e:
        print(f"Error updating user data: {e}")
        return None

@function_tool
def read_transactions(chat_id: int):
    """Reads transactions for a given chat_id from the 'vyapari_transactions' table."""
    try:
        response = supabase.table('vyapari_transactions').select('*').eq('chat_id', str(chat_id)).execute()
        # Convert chat_id back to integer for consistency if needed elsewhere,
        # but the data from DB will have it as string based on how it's stored.
        # For this function, we just return the data as is from the DB.
        return response.data
    except Exception as e:
        print(f"Error reading transactions: {e}")
        return None

def read_transactions_vanilla(chat_id: int):
    """Reads transactions for a given chat_id from the 'vyapari_transactions' table."""
    try:
        response = supabase.table('vyapari_transactions').select('*').eq('chat_id', str(chat_id)).execute()
        # Convert chat_id back to integer for consistency if needed elsewhere,
        # but the data from DB will have it as string based on how it's stored.
        # For this function, we just return the data as is from the DB.
        return response.data
    except Exception as e:
        print(f"Error reading transactions: {e}")
        return None

def download_Transactions_CSV(chat_id: int) -> str:
    """
    Fetches transactions via read_transactions(), writes them to a temporary
    CSV file, sends it to the user, then deletes the temp file.

    """
    try:

        # ── 1. Pull data from Supabase ───────────────────────────────────────
        data = read_transactions_vanilla(
            chat_id=chat_id
        )

        if not data:
            return "❌ Bhai, there is no transaction for this chat_id."

        # ── 3. Write CSV to temp file ───────────────────────────────────────
        file_name = f"transactions_{chat_id}_{datetime.now().strftime('%Y-%m')}_{datetime.now().strftime('%d%H%M')}.csv"
        with open(file_name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            writer.writerows(data)

        print(f"CSV Generated: {file_name}")
        return file_name

    except Exception as e:
        print(f"[download_Transactions_CSV] {e}")
        return "❌ Error in generating CSV."

@function_tool
def write_transaction(chat_id: int, item_name: str, quantity: int, price_per_unit: float, total_price: float, invoice_date : str, invoice_number: str, raw_message: str = None, payment_method: str = 'cash', currency: str = 'INR', customer_name: str = "", customer_details: str = ""):
    """Writes/Stores a new transaction to the 'vyapari_transactions' table.
        Expects invoice_date field in yyyy-MM-dd format. """
    try:
        # Convert date string to datetime object if it's a string
        date_obj = datetime.fromisoformat(invoice_date) if isinstance(invoice_date, str) else invoice_date

        data = {
            "chat_id": str(chat_id), # Store chat_id as string
            "item_name": item_name,
            "quantity": quantity,
            "price_per_unit": price_per_unit,
            "total_price": total_price,
            "raw_message": raw_message,
            "payment_method": payment_method,
            "currency": currency,
            "inserted_at": datetime.now(timezone.utc).isoformat(),
            "invoice_date": date_obj.isoformat(),
            "invoice_number" : invoice_number,
            "customer_name": customer_name,
            "customer_details": customer_details
        }
        response = supabase.table('vyapari_transactions').insert(data).execute()
        update_user_data(chat_id, total_price)
        
        return response.data
    except Exception as e:
        print(f"Error writing transaction: {e}")
        return None

def read_value_by_chat_id(
    table_name: str,
    chat_id: int | str,
    column_name: str
):
    """
    Read the first row (ordered by `order_by`) for the given chat_id
    from `table_name` and return the value of `column_name`.

    Args:
        table_name  : Supabase table to query.
        chat_id     : Chat identifier (int or str) to filter on.
        column_name : Column whose value you want to retrieve.

    Returns:
        The value at <column_name> in the first matching row,
        or None if no row / column found.
    """
    try:
        # 🟢  Build the query
        query = (
            supabase.table(table_name)
            .select(column_name)        # only fetch what we need
            .eq("chat_id", str(chat_id))
            .limit(1)
        )

        resp = query.execute()
        rows = resp.data or []

        if not rows:
            return "None"                 # no row for this chat_id

        row = rows[0]

        # Safeguard: column might be absent due to typo
        return row.get(column_name)

    except Exception as exc:
        print(f"[read_value_by_chat_id] {exc}")
        return None


# Enhanced styles for professional Indian invoice
styles = getSampleStyleSheet()

# Company name style
company_style = ParagraphStyle(
    'CompanyStyle',
    parent=styles['Heading1'],
    fontSize=20,
    fontName='Helvetica-Bold',
    textColor=colors.HexColor('#1a365d'),
    spaceAfter=5,
    alignment=1  # Center alignment
)

# Invoice title style
invoice_title_style = ParagraphStyle(
    'InvoiceTitle',
    parent=styles['Heading2'],
    fontSize=16,
    fontName='Helvetica-Bold',
    textColor=colors.HexColor('#2d3748'),
    spaceAfter=20,
    alignment=1
)

# Header styles
section_header_style = ParagraphStyle(
    'SectionHeader',
    parent=styles['Normal'],
    fontSize=11,
    fontName='Helvetica-Bold',
    textColor=colors.HexColor('#2d3748'),
    spaceAfter=8
)

# Normal content style
content_style = ParagraphStyle(
    'ContentStyle',
    parent=styles['Normal'],
    fontSize=10,
    fontName='Helvetica',
    textColor=colors.black,
    leftIndent=10
)

# Table header style
table_header_style = ParagraphStyle(
    'TableHeader',
    parent=styles['Normal'],
    fontSize=10,
    fontName='Helvetica-Bold',
    textColor=colors.white,
    alignment=1
)

# Amount style
amount_style = ParagraphStyle(
    'AmountStyle',
    parent=styles['Normal'],
    fontSize=10,
    fontName='Helvetica',
    alignment=2  # Right alignment
)

# Total amount style
total_amount_style = ParagraphStyle(
    'TotalAmountStyle',
    parent=styles['Normal'],
    fontSize=12,
    fontName='Helvetica-Bold',
    textColor=colors.HexColor('#1a365d'),
    alignment=2
)

# Footer style
footer_style = ParagraphStyle(
    'FooterStyle',
    parent=styles['Normal'],
    fontSize=9,
    textColor=colors.HexColor('#4a5568'),
    alignment=1,
    spaceAfter=5
)

def number_to_words(num):
        # Simplified version - you might want to use a library like 'num2words'
        return f"Rupees {num:,.2f} Only"

def generate_invoice(
    # NEW: pass a list of dicts instead of a single item
    chat_id: int,
    items,                # e.g. [{"name": "...", "qty": 2, "rate": 499.0}, …]
    date,                 # Invoice date (scalar – one per invoice)
    invoice_number=None,

    # Company details (unchanged)
    company_name="Your Company Name",
    company_address="123 Business Street, Business District",
    company_city="Mumbai, Maharashtra - 400001",
    company_phone="+91 98765 43210",
    company_email="contact@yourcompany.com",
    company_gstin="27ABCDE1234F1Z5",
    company_pan="ABCDE1234F",

    # Customer details (unchanged)
    customer_name="Customer Name",
    customer_address="Customer Address",
    customer_city="Customer City, State - PIN",
    customer_gstin="",

    # Tax details (unchanged)
    cgst_rate=9.0,
    sgst_rate=9.0,
    igst_rate=0.0,
):
    """
    Generate a professional GST invoice that can contain multiple line-items.
    `items` must be an iterable of dicts with keys:
        - name : str   (description)
        - qty  : int/float
        - rate : float (price per unit)
    """

    filename = f"invoice_{str(chat_id)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch,  bottomMargin=0.75 * inch,
    )
    elements = []

    if not invoice_number:
        invoice_number = f"INV_{str(chat_id)}/{datetime.now().strftime('%Y-%m')}/{datetime.now().strftime('%d%H%M')}"


    # Company header section
    elements.append(Paragraph(company_name, company_style))
    elements.append(Paragraph("TAX INVOICE", invoice_title_style))
    elements.append(Spacer(1, 15))
    
    # Create header table with company and invoice details
    header_data = [
        [Paragraph("<b>From:</b>", section_header_style), 
         Paragraph("<b>Invoice Details:</b>", section_header_style)],
        [Paragraph(f"{company_name}<br/>{company_address}<br/>{company_city}<br/>Phone: {company_phone}<br/>Email: {company_email}", content_style),
         Paragraph(f"<b>Invoice No:</b> {invoice_number}<br/><b>Date:</b> {date}<br/><b>GSTIN:</b> {company_gstin}<br/><b>PAN:</b> {company_pan}", content_style)]
    ]
    
    header_table = Table(header_data, colWidths=[4*inch, 3*inch])
    header_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f7fafc')),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 20))
    
    # Billing details
    billing_header = Paragraph("<b>Bill To:</b>", section_header_style)
    elements.append(billing_header)
    
    customer_info = f"{customer_name}<br/>{customer_address}<br/>{customer_city}"
    if customer_gstin:
        customer_info += f"<br/>GSTIN: {customer_gstin}"
    
    customer_table = Table([[Paragraph(customer_info, content_style)]], colWidths=[7*inch])
    customer_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 15),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
    ]))
    elements.append(customer_table)
    elements.append(Spacer(1, 20))

    # … existing header & customer code …

    # --------------------------- Items table -----------------------------------------

    items_header = [
        [
            Paragraph("S.No.", table_header_style),
            Paragraph("Description", table_header_style),
            Paragraph("Qty", table_header_style),
            Paragraph("Rate (INR.)", table_header_style),
            Paragraph("Amount (INR.)", table_header_style),
        ]
    ]

    items_data = items_header
    subtotal = 0.0  # accumulate line totals

    for idx, itm in enumerate(items, start=1):
        line_total = itm["qty"] * itm["rate"]
        subtotal += line_total

        items_data.append([
            Paragraph(str(idx), content_style),
            Paragraph(str(itm["name"]), content_style),
            Paragraph(f"{itm['qty']}", amount_style),
            Paragraph(f"{itm['rate']:,.2f}", amount_style),
            Paragraph(f"{line_total:,.2f}", amount_style),
        ])

    # ------------------------- Tax rows & totals -------------------------------------

    cgst_amount = (subtotal * cgst_rate) / 100 if cgst_rate > 0 else 0
    sgst_amount = (subtotal * sgst_rate) / 100 if sgst_rate > 0 else 0
    igst_amount = (subtotal * igst_rate) / 100 if igst_rate > 0 else 0
    total_tax   = cgst_amount + sgst_amount + igst_amount
    grand_total = subtotal + total_tax

    if cgst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"CGST @ {cgst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{cgst_amount:,.2f}", amount_style),
        ])

    if sgst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"SGST @ {sgst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{sgst_amount:,.2f}", amount_style),
        ])

    if igst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"IGST @ {igst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{igst_amount:,.2f}", amount_style),
        ])

    # build & style the table exactly as before
    items_table = Table(items_data, colWidths=[0.7 * inch, 3.5 * inch, 0.7 * inch, 1 * inch, 1.1 * inch])
    items_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d3748')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 15))

    # ------------------------- Totals section ----------------------------------------
    total_data = [
        [Paragraph("<b>Subtotal:</b>", total_amount_style), Paragraph(f"INR. {subtotal:,.2f}", total_amount_style)],
        [Paragraph("<b>Total Tax:</b>", total_amount_style), Paragraph(f"INR. {total_tax:,.2f}", total_amount_style)],
        [Paragraph("<b>Grand Total:</b>", total_amount_style), Paragraph(f"INR. {grand_total:,.2f}", total_amount_style)],
    ]
    
    total_table = Table(total_data, colWidths=[5*inch, 2*inch])
    total_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 11),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, -1), (-1, -1), 1, colors.HexColor('#2d3748')),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f7fafc')),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
    ]))
    elements.append(total_table)
    
    # Amount in words
    
    elements.append(Spacer(1, 15))
    amount_words = Paragraph(f"<b>Amount in Words:</b> {number_to_words(grand_total)}", 
                           section_header_style)
    elements.append(amount_words)
    elements.append(Spacer(1, 20))
    
    # Terms and conditions
    terms_text = """
    <b>Terms & Conditions:</b><br/>
    1. Payment is due within 30 days of invoice date.<br/>
    2. Interest @ 24% per annum will be charged on overdue amounts.<br/>
    3. All disputes subject to local jurisdiction only.<br/>
    4. Goods once sold will not be taken back.
    """
    terms = Paragraph(terms_text, content_style)
    elements.append(terms)
    elements.append(Spacer(1, 30))
    
    # Signature section
    signature_data = [
        [Paragraph("", content_style), 
         Paragraph("<b>For " + company_name + "</b><br/><br/><br/>Authorized Signatory", content_style)]
    ]
    signature_table = Table(signature_data, colWidths=[4*inch, 3*inch])
    signature_table.setStyle(TableStyle([
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
    ]))
    elements.append(signature_table)
    
    # Footer
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("Thank you for your business!", footer_style))
    elements.append(Paragraph("This is a computer generated invoice and does not require physical signature.", footer_style))
    
    # Build the PDF
    doc.build(elements)
    print(f"Invoice generated successfully: {filename} {invoice_number}")
    return filename, invoice_number