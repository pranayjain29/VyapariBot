from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from datetime import datetime
import os
from agents import Agent, Runner, trace, function_tool


styles = getSampleStyleSheet()
title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=30,
            alignment=1  # Center alignment
        )
normal_style = styles['Normal']
header_style = ParagraphStyle(
            'Header',
            parent=styles['Normal'],
            fontSize=12,
            textColor=colors.white,
            alignment=1
        )
total_style = ParagraphStyle(
            'Total',
            parent=styles['Normal'],
            fontSize=14,
            textColor=colors.black,
            alignment=2  # Right alignment
        )
        
   
def generate_invoice(item_name, quantity, price, date, invoice_number=None):
 # Create a temporary file for the PDF
        filename = f"invoice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"


        # Create the PDF document
        doc = SimpleDocTemplate(filename, pagesize=letter)
        elements = []
        
        # Add title
        elements.append(Paragraph("INVOICE", title_style))
        elements.append(Spacer(1, 20))
        
        # Add invoice details
        if not invoice_number:
            invoice_number = f"INV-{datetime.now().strftime('%Y%m%d%H%M')}"
            
        # Business details
        business_details = [
            [Paragraph("Business Name:", normal_style), Paragraph("Your Business Name", normal_style)],
            [Paragraph("Address:", normal_style), Paragraph("Your Business Address", normal_style)],
            [Paragraph("Phone:", normal_style), Paragraph("Your Phone Number", normal_style)],
            [Paragraph("Email:", normal_style), Paragraph("your.email@example.com", normal_style)],
            [Paragraph("Invoice Number:", normal_style), Paragraph(invoice_number, normal_style)],
            [Paragraph("Date:", normal_style), Paragraph(date, normal_style)],
        ]
        
        # Create business details table
        business_table = Table(business_details, colWidths=[2*inch, 4*inch])
        business_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.lightgrey),
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
        ]))
        elements.append(business_table)
        elements.append(Spacer(1, 30))
        
        # Add items table header
        header_data = [
            [Paragraph("Item", header_style),
             Paragraph("Quantity", header_style),
             Paragraph("Price", header_style),
             Paragraph("Total", header_style)]
        ]
        
        # Add items data
        items_data = header_data + [
            [Paragraph(item_name, normal_style),
             Paragraph(str(quantity), normal_style),
             Paragraph(f"₹{price:,}", normal_style),
             Paragraph(f"₹{quantity * price:,}", normal_style)]
        ]
        
        # Create items table
        items_table = Table(items_data, colWidths=[3*inch, 1*inch, 1.5*inch, 1.5*inch])
        items_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
        ]))
        elements.append(items_table)
        elements.append(Spacer(1, 30))
        
        # Add total
        total_data = [[
            Paragraph("Total Amount:", total_style),
            Paragraph(f"₹{quantity * price:,}", total_style)
        ]]
        total_table = Table(total_data, colWidths=[4*inch, 3*inch])
        total_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (0, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 14),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BACKGROUND', (0, 0), (-1, -1), colors.lightgrey),
        ]))
        elements.append(total_table)
        
        # Add footer
        elements.append(Spacer(1, 50))
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=10,
            textColor=colors.grey,
            alignment=1
        )
        elements.append(Paragraph("Thank you for your business!", footer_style))
        
        # Build the PDF
        doc.build(elements)
        return filename
