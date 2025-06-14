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
def write_transaction(chat_id: int, item_name: str, quantity: int, price_per_unit: float, total_price: float, invoice_date : str, raw_message: str = None, payment_method: str = 'cash', currency: str = 'INR'):
    """Writes/Stores a new transaction to the 'vyapari_transactions' table."""
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
            "date": date_obj.isoformat()
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

def generate_invoice(
    # Item details
    item_name, 
    quantity, 
    price, 
    date, 
    invoice_number=None,
    
    # Company details
    company_name="Your Company Name",
    company_address="123 Business Street, Business District",
    company_city="Mumbai, Maharashtra - 400001",
    company_phone="+91 98765 43210",
    company_email="contact@yourcompany.com",
    company_gstin="27ABCDE1234F1Z5",
    company_pan="ABCDE1234F",
    
    # Customer details
    customer_name="Customer Name",
    customer_address="Customer Address",
    customer_city="Customer City, State - PIN",
    customer_gstin="",
    
    # Tax details
    cgst_rate=9.0,  # Central GST %
    sgst_rate=9.0,  # State GST %
    igst_rate=0.0   # Integrated GST % (for inter-state)
):
    """
    Generate a professional invoice for Indian businesses with GST compliance
    """
    
    # Create filename with timestamp
    filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    
    # Use A4 page size (standard in India)
    doc = SimpleDocTemplate(filename, pagesize=A4, 
                          leftMargin=0.75*inch, rightMargin=0.75*inch,
                          topMargin=0.75*inch, bottomMargin=0.75*inch)
    elements = []
    
    # Generate invoice number if not provided
    if not invoice_number:
        invoice_number = f"INV/{datetime.now().strftime('%Y-%m')}/{datetime.now().strftime('%d%H%M')}"
    
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
    
    # Items table
    items_header = [
        [Paragraph("S.No.", table_header_style),
         Paragraph("Description", table_header_style),
         Paragraph("Qty", table_header_style),
         Paragraph("Rate (INR.)", table_header_style),
         Paragraph("Amount (INR.)", table_header_style)]
    ]
    
    # Calculate amounts
    line_total = quantity * price
    cgst_amount = (line_total * cgst_rate) / 100 if cgst_rate > 0 else 0
    sgst_amount = (line_total * sgst_rate) / 100 if sgst_rate > 0 else 0
    igst_amount = (line_total * igst_rate) / 100 if igst_rate > 0 else 0
    total_tax = cgst_amount + sgst_amount + igst_amount
    grand_total = line_total + total_tax
    
    # Items data
    items_data = items_header + [
        [Paragraph("1", content_style),
         Paragraph(item_name, content_style),
         Paragraph(str(quantity), amount_style),
         Paragraph(f"{price:,.2f}", amount_style),
         Paragraph(f"{line_total:,.2f}", amount_style)]
    ]
    
    # Add tax rows
    if cgst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"CGST @ {cgst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{cgst_amount:,.2f}", amount_style)
        ])
    
    if sgst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"SGST @ {sgst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{sgst_amount:,.2f}", amount_style)
        ])
    
    if igst_amount > 0:
        items_data.append([
            Paragraph("", content_style),
            Paragraph(f"IGST @ {igst_rate}%", content_style),
            Paragraph("", content_style),
            Paragraph("", content_style),
            Paragraph(f"{igst_amount:,.2f}", amount_style)
        ])
    
    # Create items table
    items_table = Table(items_data, colWidths=[0.7*inch, 3.5*inch, 0.7*inch, 1*inch, 1.1*inch])
    
    # Table styling
    table_style = [
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),  # Description column left-aligned
        ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),  # Numbers right-aligned
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d3748')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7fafc')]),
    ]
    
    items_table.setStyle(TableStyle(table_style))
    elements.append(items_table)
    elements.append(Spacer(1, 15))
    
    # Total section
    total_data = [
        [Paragraph("<b>Subtotal:</b>", total_amount_style), 
         Paragraph(f"INR. {line_total:,.2f}", total_amount_style)],
        [Paragraph("<b>Total Tax:</b>", total_amount_style), 
         Paragraph(f"INR. {total_tax:,.2f}", total_amount_style)],
        [Paragraph("<b>Grand Total:</b>", total_amount_style), 
         Paragraph(f"INR. {grand_total:,.2f}", total_amount_style)]
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
    def number_to_words(num):
        # Simplified version - you might want to use a library like 'num2words'
        return f"Rupees {num:,.2f} Only"
    
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
    print(f"Invoice generated successfully: {filename}")
    return filename