from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch, cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
import os
from agents import Agent, Runner, trace, function_tool

# Initialize Supabase client
url: str = os.environ.get("SUPABASE_URL_KEY")
key: str = os.environ.get("SUPABASE_API_KEY")
supabase: Client = create_client(url, key)

print("Supabase client created")

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

@function_tool
def write_transaction(chat_id: int, item_name: str, quantity: int, price_per_unit: float, total_price: float, invoice_date : str, invoice_number: str, raw_message: str = None, payment_method: str = 'cash', currency: str = 'INR'):
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
            "invoice_number" : invoice_number
        }
        response = supabase.table('vyapari_transactions').insert(data).execute()
        return response.data
    except Exception as e:
        print(f"Error writing transaction: {e}")
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

    filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch,  bottomMargin=0.75 * inch,
    )
    elements = []

    if not invoice_number:
        invoice_number = f"INV/{datetime.now().strftime('%Y-%m')}/{datetime.now().strftime('%d%H%M')}"

    # -------------- company, header, customer sections remain unchanged --------------

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