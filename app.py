#!/usr/bin/env python3
"""
District Report Generator Web App V2
Flask application with MongoDB PDF storage, Indeed analysis, and meeting prep
"""

import os
import json
import base64
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file, Response
from pymongo import MongoClient
from anthropic import Anthropic
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.colors import HexColor
import re
from io import BytesIO

# Load environment variables
load_dotenv()

app = Flask(__name__)

class DistrictReportGenerator:
    def __init__(self, mongodb_uri, anthropic_api_key):
        """Initialize with MongoDB and Anthropic connections"""
        self.mongo_client = MongoClient(mongodb_uri)
        self.db = self.mongo_client['applitrack-job-scraper']
        self.anthropic = Anthropic(api_key=anthropic_api_key)
        
    def search_districts(self, search_text=None, state=None, county=None, min_enrollment=None, max_enrollment=None):
        """Search districts with filters"""
        query = {}
        
        if search_text:
            query["name"] = {"$regex": search_text, "$options": "i"}
        
        if state:
            query["state"] = state
            
        if county:
            query["county"] = {"$regex": county, "$options": "i"}
            
        if min_enrollment is not None or max_enrollment is not None:
            query["totalEnrollment"] = {}
            if min_enrollment is not None:
                query["totalEnrollment"]["$gte"] = min_enrollment
            if max_enrollment is not None:
                query["totalEnrollment"]["$lte"] = max_enrollment
        
        districts = list(self.db.districts.find(query).limit(100))
        
        # Convert ObjectId to string for JSON serialization
        for district in districts:
            district['_id'] = str(district['_id'])
            
        return districts
    
    def get_all_states(self):
        """Get list of all states in database"""
        return sorted(self.db.districts.distinct("state"))
    
    def get_district_basics(self, district_name):
        """Retrieve basic district info from MongoDB districts collection"""
        district = self.db.districts.find_one(
            {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
        )
        
        if not district:
            return None
        
        # Get district ID for aggregating school data
        district_id = district.get('_id')
        
        # Aggregate demographics from schools
        demographics = self.calculate_district_demographics(district_id)
        
        return {
            "name": district.get("name", "Unknown"),
            "leaId": district.get("leaId", ""),
            "enrollment": district.get("totalEnrollment", 0),
            "num_schools": district.get("totalSchools", 0),
            "state": district.get("state", ""),
            "county": district.get("county", ""),
            "total_jobs": district.get("totalJobs", 0),
            "is_target_client": district.get("isTargetClient", False),
            "is_radar_client": district.get("isRadarClient", False),
            "coordinates": district.get("coordinates", {}),
            "demographics": demographics
        }
    
    def calculate_district_demographics(self, district_id):
        """Aggregate demographic data from schools to district level"""
        schools = list(self.db.schools.find({"districtId": district_id}))
        
        if not schools:
            return {
                "free_reduced_lunch_pct": None,
                "white_pct": None,
                "minority_pct": None,
                "total_frl": 0,
                "total_enrollment": 0
            }
        
        total_frl = 0
        total_enrollment = 0
        total_white = 0
        total_minority = 0
        
        for school in schools:
            # Free/Reduced Lunch
            frl = school.get("freeReducedLunch", {})
            frl_total = frl.get("total", 0)
            if frl_total > 0:  # Only count if valid data
                total_frl += frl_total
            
            # Enrollment
            enrollment = school.get("enrollment", {})
            school_total = enrollment.get("total", 0)
            if school_total > 0:
                total_enrollment += school_total
            
            # Demographics
            demographics = school.get("demographics", {})
            white = demographics.get("white", 0)
            total_white += white
            
            # Minority = all non-white
            minority = (
                demographics.get("americanIndian", 0) +
                demographics.get("asian", 0) +
                demographics.get("black", 0) +
                demographics.get("hispanic", 0) +
                demographics.get("pacificIslander", 0) +
                demographics.get("twoOrMore", 0)
            )
            total_minority += minority
        
        # Calculate percentages
        frl_pct = (total_frl / total_enrollment * 100) if total_enrollment > 0 else None
        total_demographic = total_white + total_minority
        white_pct = (total_white / total_demographic * 100) if total_demographic > 0 else None
        minority_pct = (total_minority / total_demographic * 100) if total_demographic > 0 else None
        
        return {
            "free_reduced_lunch_pct": round(frl_pct, 1) if frl_pct else None,
            "white_pct": round(white_pct, 1) if white_pct else None,
            "minority_pct": round(minority_pct, 1) if minority_pct else None,
            "total_frl": total_frl,
            "total_enrollment": total_enrollment
        }
    
    def find_similar_districts(self, district_data, limit=3):
        """Find similar districts based on enrollment and location"""
        if not district_data:
            return []
        
        enrollment = district_data.get("enrollment", 0)
        state = district_data.get("state", "")
        county = district_data.get("county", "")
        
        similar = list(self.db.districts.find({
            "state": state,
            "county": county,
            "name": {"$ne": district_data["name"]},
            "totalEnrollment": {
                "$gte": enrollment * 0.5,
                "$lte": enrollment * 1.5
            }
        }).limit(limit))
        
        if len(similar) < limit:
            additional = self.db.districts.find({
                "state": state,
                "county": {"$ne": county},
                "name": {"$ne": district_data["name"]},
                "totalEnrollment": {
                    "$gte": enrollment * 0.6,
                    "$lte": enrollment * 1.4
                }
            }).limit(limit - len(similar))
            similar.extend(list(additional))
        
        return [
            {
                "name": d.get("name"),
                "enrollment": d.get("totalEnrollment"),
                "num_schools": d.get("totalSchools"),
                "county": d.get("county"),
                "total_jobs": d.get("totalJobs", 0),
            }
            for d in similar
        ]
    
    def scrape_job_website_with_indeed(self, district_name, state, similar_districts):
        """Enhanced job scraping with Indeed analysis and financial health check"""
        prompt = f"""Search the web to analyze job postings and financial health for {district_name} in {state}.

PART 1 - District Career Page:
1. Find their official careers/employment page URL
2. Count open positions
3. List types of roles (teachers, admin, support staff, etc.)
4. Note urgent hiring needs or hard-to-fill positions

PART 2 - Indeed Analysis:
Search Indeed for "{district_name} {state}" jobs and provide:
1. How many listings are on Indeed?
2. Do any have "Easy Apply" enabled? (this is important - note specifically)
3. What's the average posting age (how recent are they)?

PART 3 - Competitive Analysis:
Compare {district_name} to these similar districts on Indeed:
{json.dumps([d['name'] for d in similar_districts], indent=2)}

For each district, check Indeed and note:
- Number of open postings
- Use of "Easy Apply"
- Competitiveness score (1-10, where 10 = most competitive/aggressive recruiting)

Then provide an overall competitiveness score for {district_name} vs. similar districts.

PART 4 - Financial Health Assessment:
Research {district_name}'s financial situation:
1. Search for recent budget reports, financial statements, or audit reports
2. Look for news about budget deficits, surpluses, or financial challenges
3. Check for mentions of bond measures or funding initiatives
4. Find student-per-teacher ratios or per-pupil spending if available
5. Provide a Financial Health Score (1-10, where 10 = excellent financial health)
   - Consider: recent budget news, stability, per-pupil spending trends
   - Include brief reasoning for the score

Format with clear sections and include relevant URLs."""

        try:
            message = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=3500,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }],
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            job_info = ""
            for block in message.content:
                if block.type == "text":
                    job_info += block.text + "\n"
            
            return job_info.strip()
        
        except Exception as e:
            return f"Unable to retrieve job postings analysis. Error: {str(e)}"
    
    def research_contacts(self, contact_names, district_name):
        """Research contacts for meeting preparation"""
        if not contact_names or not contact_names.strip():
            return None
            
        prompt = f"""Research these contacts from {district_name} for meeting preparation:

{contact_names}

For each person, please find:
1. Current role/title at {district_name}
2. LinkedIn profile (URL if available)
3. Professional background and experience
4. Recent news mentions or accomplishments
5. Are they likely a key decision-maker for hiring/recruitment? (Yes/No and why)
6. Any public statements about staffing or education priorities

Provide specific URLs and sources for all information found."""

        try:
            message = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=3000,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }],
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            
            contact_info = ""
            for block in message.content:
                if block.type == "text":
                    contact_info += block.text + "\n"
            
            return contact_info.strip()
        
        except Exception as e:
            return f"Unable to research contacts. Error: {str(e)}"
    
    def analyze_with_claude(self, district_name, district_data, similar_districts, job_scrape_info, contact_research=None):
        """Enhanced Claude analysis with references"""
        
        contact_section = ""
        if contact_research:
            contact_section = f"""
MEETING CONTACTS RESEARCH:
{contact_research}
"""
        
        demographics = district_data.get('demographics', {})
        frl_pct = demographics.get('free_reduced_lunch_pct', 'N/A')
        minority_pct = demographics.get('minority_pct', 'N/A')
        
        prompt = f"""Create a concise, well-formatted district profile for {district_name}.

BASIC INFORMATION:
- Enrollment: {district_data['enrollment']:,}
- Schools: {district_data['num_schools']}
- Location: {district_data['county']}, {district_data['state']}
- LEA ID: {district_data['leaId']}
- Free/Reduced Lunch: {frl_pct}%
- Minority Population: {minority_pct}%

SIMILAR DISTRICTS:
{json.dumps(similar_districts, indent=2)}

JOB POSTINGS & FINANCIAL ANALYSIS:
{job_scrape_info}

{contact_section}

Search for recent school board meeting minutes/agendas from {district_name} focusing on:
- Staffing challenges or teacher shortages
- Recruitment initiatives
- Budget/personnel issues
- External recruiting partnerships

CRITICAL: For every claim you make, include the source URL in brackets like [Source: https://example.com]

Create sections (use these exact headers):
1. DISTRICT OVERVIEW (include key demographics and financial health context)
2. CURRENT HIRING LANDSCAPE
3. INDEED COMPETITIVENESS ANALYSIS
4. FINANCIAL HEALTH ASSESSMENT (expand on financial score from research)
5. SCHOOL BOARD INSIGHTS{"" if not contact_research else ""}
{"6. MEETING CONTACTS INTEL" if contact_research else ""}
{"7. SIMILAR DISTRICTS COMPARISON" if contact_research else "6. SIMILAR DISTRICTS COMPARISON"}
{"8. SALES APPROACH" if contact_research else "7. SALES APPROACH"}

In SALES APPROACH, consider:
- How FRL% and demographics affect recruiting challenges
- How financial health impacts their ability to hire/retain
- Budget constraints or opportunities

Keep each section concise (3-4 sentences max). Focus on actionable insights. Include source URLs."""

        message = self.anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search"
            }],
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        analysis = ""
        for block in message.content:
            if block.type == "text":
                analysis += block.text + "\n"
        
        return analysis.strip()
    
    def generate_report(self, district_name, contact_names=None):
        """Main method to generate complete district report"""
        district_data = self.get_district_basics(district_name)
        
        if not district_data:
            return None
        
        similar_districts = self.find_similar_districts(district_data)
        
        # Track API calls for cost estimation
        api_calls = []
        
        # Enhanced job scraping with Indeed analysis
        try:
            print(f"Starting job scraping for {district_name}")
            job_scrape_info = self.scrape_job_website_with_indeed(
                district_name, 
                district_data['state'],
                similar_districts
            )
            api_calls.append({"type": "job_scraping", "tokens": 3500})
            print("Job scraping complete")
        except Exception as e:
            print(f"Job scraping error: {str(e)}")
            job_scrape_info = f"Unable to retrieve job information. Error: {str(e)}"
        
        # Research contacts if provided
        contact_research = None
        if contact_names:
            try:
                print(f"Starting contact research")
                contact_research = self.research_contacts(contact_names, district_name)
                api_calls.append({"type": "contact_research", "tokens": 3000})
                print("Contact research complete")
            except Exception as e:
                print(f"Contact research error: {str(e)}")
                contact_research = f"Unable to research contacts. Error: {str(e)}"
        
        # Generate analysis
        try:
            print(f"Starting Claude analysis")
            claude_analysis = self.analyze_with_claude(
                district_name, 
                district_data, 
                similar_districts,
                job_scrape_info,
                contact_research
            )
            api_calls.append({"type": "analysis", "tokens": 4000})
            print("Claude analysis complete")
        except Exception as e:
            print(f"Claude analysis error: {str(e)}")
            claude_analysis = f"Unable to generate full analysis. Error: {str(e)}"
        
        # Calculate estimated cost
        total_tokens = sum(call["tokens"] for call in api_calls)
        input_tokens = int(total_tokens * 0.6)
        output_tokens = int(total_tokens * 0.4)
        estimated_cost = (input_tokens / 1_000_000 * 3) + (output_tokens / 1_000_000 * 15)
        
        report = {
            "district_name": district_name,
            "generated_at": datetime.now().isoformat(),
            "basic_data": district_data,
            "similar_districts": similar_districts,
            "job_scrape_info": job_scrape_info,
            "contact_research": contact_research,
            "claude_analysis": claude_analysis,
            "api_calls": api_calls,
            "estimated_cost": round(estimated_cost, 3)
        }
        
        return report
    
    def generate_pdf(self, report):
        """Generate PDF with improved formatting"""
        if not report:
            return None
        
        # Create PDF in memory
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter,
                              topMargin=0.75*inch, bottomMargin=0.75*inch)
        story = []
        styles = getSampleStyleSheet()
        
        # Custom styles with bold headers
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=20,
            textColor=HexColor('#1a1a1a'),
            spaceAfter=20,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=13,
            textColor=HexColor('#2563eb'),
            spaceAfter=8,
            spaceBefore=12,
            fontName='Helvetica-Bold'
        )
        
        body_style = ParagraphStyle(
            'CustomBody',
            parent=styles['Normal'],
            fontSize=10,
            leading=14,
            spaceAfter=8
        )
        
        # Title
        story.append(Paragraph(f"{report['district_name']}", title_style))
        gen_date = datetime.fromisoformat(report['generated_at']).strftime("%B %d, %Y")
        story.append(Paragraph(f"<i>Generated: {gen_date}</i>", body_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Basic Information
        story.append(Paragraph("<b>BASIC INFORMATION</b>", heading_style))
        data = report['basic_data']
        demographics = data.get('demographics', {})
        
        # Format demographics
        frl_text = f"{demographics.get('free_reduced_lunch_pct', 'N/A')}%" if demographics.get('free_reduced_lunch_pct') else "N/A"
        white_text = f"{demographics.get('white_pct', 'N/A')}%" if demographics.get('white_pct') else "N/A"
        minority_text = f"{demographics.get('minority_pct', 'N/A')}%" if demographics.get('minority_pct') else "N/A"
        
        basic_info = f"""<b>District Profile:</b> Enrollment: {data['enrollment']:,} | Schools: {data['num_schools']} | Location: {data['county']}, {data['state']}<br/>
<b>Demographics:</b> Free/Reduced Lunch: {frl_text} | White: {white_text} | Minority: {minority_text}<br/>
<b>Status:</b> Target Client: {'Yes' if data['is_target_client'] else 'No'} | Radar Client: {'Yes' if data['is_radar_client'] else 'No'}"""
        story.append(Paragraph(basic_info, body_style))
        story.append(Spacer(1, 0.15*inch))
        
        # Similar Districts (condensed)
        story.append(Paragraph("<b>SIMILAR DISTRICTS</b>", heading_style))
        similar_text = " | ".join([
            f"{d['name']} ({d['enrollment']:,})" 
            for d in report['similar_districts']
        ])
        story.append(Paragraph(similar_text, body_style))
        story.append(Spacer(1, 0.15*inch))
        
        # Parse and format the Claude analysis with bold headers
        analysis_text = report['claude_analysis']
        
        # Split by common section headers and format
        lines = analysis_text.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                story.append(Spacer(1, 0.1*inch))
                continue
                
            # Check if it's a header (all caps or starts with number)
            if (line.isupper() and len(line) < 50) or re.match(r'^\d+\.', line):
                story.append(Paragraph(f"<b>{line}</b>", heading_style))
            else:
                # Convert markdown links to HTML links
                line = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'<a href="\2" color="blue">\1</a>', line)
                # Convert [Source: URL] to clickable links
                line = re.sub(r'\[Source: ([^\]]+)\]', r'<a href="\1" color="blue">[Source]</a>', line)
                story.append(Paragraph(line, body_style))
        
        # Build PDF
        doc.build(story)
        
        pdf_data = buffer.getvalue()
        buffer.close()
        
        return pdf_data
    
    def save_report_to_db(self, report, pdf_data):
        """Save report and PDF to MongoDB"""
        district_name = report["district_name"].replace(" ", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{district_name}_{timestamp}.pdf"
        
        # Store in MongoDB
        report_doc = {
            "filename": filename,
            "district_name": report["district_name"],
            "generated_at": datetime.now(),
            "pdf_data": base64.b64encode(pdf_data).decode('utf-8'),
            "report_json": report
        }
        
        self.db.generated_reports.insert_one(report_doc)
        
        return filename
    
    def get_all_reports(self):
        """Retrieve all reports from MongoDB"""
        reports = list(self.db.generated_reports.find().sort("generated_at", -1))
        
        return [
            {
                "filename": r["filename"],
                "district_name": r["district_name"],
                "created": r["generated_at"].isoformat(),
                "size": len(base64.b64decode(r["pdf_data"])),
                "estimated_cost": r["report_json"].get("estimated_cost", 0)
            }
            for r in reports
        ]
    
    def get_report_pdf(self, filename):
        """Retrieve PDF from MongoDB"""
        report = self.db.generated_reports.find_one({"filename": filename})
        
        if not report:
            return None
        
        return base64.b64decode(report["pdf_data"])

# Initialize generator
mongodb_uri = os.getenv("MONGODB_URI")
anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
generator = DistrictReportGenerator(mongodb_uri, anthropic_api_key)

@app.route('/')
def index():
    """Main page"""
    states = generator.get_all_states()
    return render_template('index.html', states=states)

@app.route('/api/search', methods=['POST'])
def search():
    """Search districts API endpoint"""
    data = request.json
    
    districts = generator.search_districts(
        search_text=data.get('search_text'),
        state=data.get('state'),
        county=data.get('county'),
        min_enrollment=data.get('min_enrollment'),
        max_enrollment=data.get('max_enrollment')
    )
    
    # Add demographics to each district
    for district in districts:
        district_id = district.get('_id')
        if district_id:
            from bson import ObjectId
            demographics = generator.calculate_district_demographics(ObjectId(district_id))
            district['demographics'] = demographics
    
    return jsonify(districts)

@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    """Generate report for a district"""
    data = request.json
    district_name = data.get('district_name')
    contact_names = data.get('contact_names')
    
    if not district_name:
        return jsonify({"error": "District name required"}), 400
    
    try:
        print(f"Starting report generation for {district_name}")
        report = generator.generate_report(district_name, contact_names)
        
        if not report:
            print(f"District not found: {district_name}")
            return jsonify({"error": "District not found"}), 404
        
        print(f"Report generated, creating PDF...")
        # Generate PDF
        pdf_data = generator.generate_pdf(report)
        
        if not pdf_data:
            print("PDF generation failed")
            return jsonify({"error": "PDF generation failed"}), 500
        
        print(f"Saving to MongoDB...")
        # Save to MongoDB
        pdf_filename = generator.save_report_to_db(report, pdf_data)
        
        print(f"Report complete: {pdf_filename}")
        
        return jsonify({
            "success": True,
            "pdf_filename": pdf_filename,
            "report": report  # Include full report for demo section
        })
    
    except Exception as e:
        print(f"Error generating report: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/reports')
def list_reports():
    """List all generated reports from MongoDB"""
    try:
        reports = generator.get_all_reports()
        return jsonify(reports)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/download/<filename>')
def download_report(filename):
    """Download a PDF report from MongoDB"""
    try:
        pdf_data = generator.get_report_pdf(filename)
        
        if not pdf_data:
            return jsonify({"error": "File not found"}), 404
        
        return Response(
            pdf_data,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment;filename={filename}'}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/report-preview/<filename>')
def preview_report(filename):
    """Get report JSON data for preview"""
    try:
        report = generator.db.generated_reports.find_one({"filename": filename})
        
        if not report:
            return jsonify({"error": "Report not found"}), 404
        
        # Return the report JSON without the PDF data
        return jsonify({
            "filename": report["filename"],
            "district_name": report["district_name"],
            "generated_at": report["generated_at"].isoformat(),
            "report_json": report["report_json"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/cost-stats')
def cost_stats():
    """Get total cost statistics"""
    try:
        reports = list(generator.db.generated_reports.find())
        
        if not reports:
            return jsonify({
                "total_reports": 0,
                "total_cost": 0,
                "avg_cost_per_report": 0
            })
        
        total_cost = sum(r["report_json"].get("estimated_cost", 0) for r in reports)
        
        return jsonify({
            "total_reports": len(reports),
            "total_cost": round(total_cost, 2),
            "avg_cost_per_report": round(total_cost / len(reports), 3)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)