#!/usr/bin/env python3
"""
District Report Generator Web App
Flask application for searching districts and generating PDF reports
"""

import os
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from pymongo import MongoClient
from anthropic import Anthropic
from dotenv import load_dotenv
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER
import re

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
        }
    
    def find_similar_districts(self, district_data, limit=3):
        """Find similar districts based on enrollment and location"""
        if not district_data:
            return []
        
        enrollment = district_data.get("enrollment", 0)
        state = district_data.get("state", "")
        county = district_data.get("county", "")
        
        # Find districts with similar characteristics
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
    
    def scrape_job_website(self, district_name, state):
        """Use Claude API with web search to find and scrape job postings"""
        prompt = f"""Search the web to find the careers/jobs page for {district_name} in {state}.

Please:
1. Find their official careers or employment page URL
2. Tell me how many open positions they currently have
3. List the types of roles they're hiring for (teachers, admin, support staff, etc.)
4. Note if the page mentions any urgent hiring needs or hard-to-fill positions

If you can't find a careers page, search for "{district_name} {state} jobs" or "{district_name} employment" to see if they post jobs elsewhere."""

        try:
            message = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
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
            return f"Unable to retrieve current job postings from website. Error: {str(e)}"
    
    def analyze_with_claude(self, district_name, district_data, similar_districts, job_scrape_info):
        """Use Claude API to analyze board meetings and generate report"""
        
        prompt = f"""I need you to create a 1-page district profile for {district_name}.

BASIC INFORMATION:
- Total Enrollment: {district_data['enrollment']:,}
- Number of Schools: {district_data['num_schools']}
- Job Postings in Database: {district_data['total_jobs']}
- Location: {district_data['county']}, {district_data['state']}
- LEA ID: {district_data['leaId']}

SIMILAR DISTRICTS:
{json.dumps(similar_districts, indent=2)}

CURRENT JOB POSTINGS (from website):
{job_scrape_info}

Please help me gather one more critical insight:

**School Board Meetings**: Search for recent school board meeting minutes or agendas from {district_name}. Have they mentioned:
- Staffing challenges or teacher shortages
- Recruitment initiatives or hiring difficulties  
- Budget issues related to personnel
- Any mentions of partnering with external recruiting firms

Look for meetings from the past 2-3 years if available.

Then create a structured summary with these sections:
1. District Overview (size, key facts)
2. Current Hiring Needs (based on job postings)
3. School Board Staffing Discussions (what you found in meeting notes)
4. Similar Districts Comparison
5. Sales Approach Recommendations (based on all the above)

Format this as a professional brief that would help our sales team prepare for outreach."""

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
    
    def generate_report(self, district_name):
        """Main method to generate complete district report"""
        district_data = self.get_district_basics(district_name)
        
        if not district_data:
            return None
        
        similar_districts = self.find_similar_districts(district_data)
        job_scrape_info = self.scrape_job_website(district_name, district_data['state'])
        claude_analysis = self.analyze_with_claude(
            district_name, 
            district_data, 
            similar_districts,
            job_scrape_info
        )
        
        report = {
            "district_name": district_name,
            "generated_at": datetime.now().isoformat(),
            "basic_data": district_data,
            "similar_districts": similar_districts,
            "job_scrape_info": job_scrape_info,
            "claude_analysis": claude_analysis
        }
        
        return report
    
    def generate_pdf(self, report, output_dir="reports"):
        """Generate PDF from report data"""
        if not report:
            return None
        
        os.makedirs(output_dir, exist_ok=True)
        
        district_name = report["district_name"].replace(" ", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{output_dir}/{district_name}_{timestamp}.pdf"
        
        # Create PDF
        doc = SimpleDocTemplate(filename, pagesize=letter)
        story = []
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor='#1a1a1a',
            spaceAfter=30,
            alignment=TA_CENTER
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor='#333333',
            spaceAfter=12,
            spaceBefore=12
        )
        
        # Title
        story.append(Paragraph(f"District Report: {report['district_name']}", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Generated date
        gen_date = datetime.fromisoformat(report['generated_at']).strftime("%B %d, %Y at %I:%M %p")
        story.append(Paragraph(f"<i>Generated: {gen_date}</i>", styles['Normal']))
        story.append(Spacer(1, 0.3*inch))
        
        # Basic Information
        story.append(Paragraph("Basic Information", heading_style))
        data = report['basic_data']
        basic_info = f"""
        <b>LEA ID:</b> {data['leaId']}<br/>
        <b>Enrollment:</b> {data['enrollment']:,}<br/>
        <b>Number of Schools:</b> {data['num_schools']}<br/>
        <b>Job Postings in Database:</b> {data['total_jobs']}<br/>
        <b>Location:</b> {data['county']}, {data['state']}<br/>
        <b>Target Client:</b> {'Yes' if data['is_target_client'] else 'No'}<br/>
        <b>Radar Client:</b> {'Yes' if data['is_radar_client'] else 'No'}
        """
        story.append(Paragraph(basic_info, styles['Normal']))
        story.append(Spacer(1, 0.2*inch))
        
        # Similar Districts
        story.append(Paragraph("Similar Districts", heading_style))
        for district in report['similar_districts']:
            similar_text = f"<b>• {district['name']}</b> ({district['county']})<br/>"
            similar_text += f"&nbsp;&nbsp;Enrollment: {district['enrollment']:,}, Schools: {district['num_schools']}, Jobs: {district['total_jobs']}"
            story.append(Paragraph(similar_text, styles['Normal']))
            story.append(Spacer(1, 0.1*inch))
        
        story.append(Spacer(1, 0.2*inch))
        
        # Current Job Postings
        story.append(Paragraph("Current Job Postings (from website)", heading_style))
        job_text = report['job_scrape_info'].replace('\n', '<br/>')
        story.append(Paragraph(job_text, styles['Normal']))
        story.append(Spacer(1, 0.2*inch))
        
        # Claude Analysis
        story.append(Paragraph("Detailed Analysis", heading_style))
        analysis_text = report['claude_analysis'].replace('\n', '<br/>')
        story.append(Paragraph(analysis_text, styles['Normal']))
        
        # Build PDF
        doc.build(story)
        
        return filename

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
    
    return jsonify(districts)

@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    """Generate report for a district"""
    data = request.json
    district_name = data.get('district_name')
    
    if not district_name:
        return jsonify({"error": "District name required"}), 400
    
    try:
        report = generator.generate_report(district_name)
        
        if not report:
            return jsonify({"error": "District not found"}), 404
        
        # Generate PDF
        pdf_path = generator.generate_pdf(report)
        
        # Get just the filename
        pdf_filename = os.path.basename(pdf_path)
        
        return jsonify({
            "success": True,
            "pdf_filename": pdf_filename,
            "report": report
        })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/reports')
def list_reports():
    """List all generated reports"""
    reports_dir = "reports"
    
    if not os.path.exists(reports_dir):
        return jsonify([])
    
    files = []
    for filename in os.listdir(reports_dir):
        if filename.endswith('.pdf'):
            filepath = os.path.join(reports_dir, filename)
            files.append({
                "filename": filename,
                "created": datetime.fromtimestamp(os.path.getctime(filepath)).isoformat(),
                "size": os.path.getsize(filepath)
            })
    
    # Sort by creation date, newest first
    files.sort(key=lambda x: x['created'], reverse=True)
    
    return jsonify(files)

@app.route('/api/download/<filename>')
def download_report(filename):
    """Download a PDF report"""
    filepath = os.path.join("reports", filename)
    
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found"}), 404
    
    return send_file(filepath, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)