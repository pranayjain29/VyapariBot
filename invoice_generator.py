from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from datetime import datetime
import os

class InvoiceGenerator:
    def __init__(self):
        self.styles = getSampleStyleSheet()
        self.title_style = ParagraphStyle(
            'CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            spaceAfter=30
        )
        self.normal_style = self.styles['Normal']
        
    def generate_invoice(self, item_name, quantity, price, date, invoice_number=None):
        # Create a temporary file for the PDF
        filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        
        # Create the PDF document
        doc = SimpleDocTemplate(filename, pagesize=letter)
        elements = []
        
        # Add title
        elements.append(Paragraph("INVOICE", self.title_style))
        
        # Add invoice details
        if not invoice_number:
            invoice_number = f"INV-{datetime.now().strftime('%Y%m%d%H%M')}"
            
        # Business details
        business_details = [
            ["Business Name:", "Your Business Name"],
            ["Address:", "Your Business Address"],
            ["Phone:", "Your Phone Number"],
            ["Email:", "your.email@example.com"],
            ["Invoice Number:", invoice_number],
            ["Date:", date],
        ]
        
        # Create business details table
        business_table = Table(business_details, colWidths=[2*inch, 4*inch])
        business_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ]))
        elements.append(business_table)
        elements.append(Spacer(1, 20))
        
        # Add items table
        items_data = [
            ["Item", "Quantity", "Price", "Total"],
            [item_name, str(quantity), f"₹{price}", f"₹{quantity * price}"]
        ]
        
        items_table = Table(items_data, colWidths=[3*inch, 1*inch, 1.5*inch, 1.5*inch])
        items_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ]))
        elements.append(items_table)
        elements.append(Spacer(1, 20))
        
        # Add total
        total_data = [["Total Amount:", f"₹{quantity * price}"]]
        total_table = Table(total_data, colWidths=[4*inch, 3*inch])
        total_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 14),
        ]))
        elements.append(total_table)
        
        # Build the PDF
        doc.build(elements)
        return filename

    def cleanup(self, filename):
        """Remove the temporary PDF file"""
        try:
            os.remove(filename)
        except:
            pass 