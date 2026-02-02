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
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.colors import HexColor, black, white
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
    
    def generate_demo_script(self, district_name):
        """
        Generate demo script using MongoDB data + ONE targeted web search for board minutes
        Handles cases where job data might not be available
        
        Perfect for: Sales calls, demo prep with recent board context
        Cost: ~$0.006, Time: ~15 seconds
        """
        
        try:
            # Get all data from MongoDB
            district_data = self.get_district_basics(district_name)
            if not district_data:
                return None
            
            similar_districts = self.find_similar_districts(district_data)
            demographics = district_data.get('demographics', {})
            
            # Get schools breakdown for more context
            try:
                from bson import ObjectId
                # Handle _id conversion carefully
                district_id = district_data.get('_id')
                if isinstance(district_id, str):
                    district_id = ObjectId(district_id)
                
                schools = list(self.db.schools.find({"districtId": district_id}))
            except Exception as e:
                print(f"Warning: Could not fetch schools data: {str(e)}")
                schools = []
            
            # Analyze school types
            elementary = [s for s in schools if 'elementary' in s.get('name', '').lower()]
            middle = [s for s in schools if 'middle' in s.get('name', '').lower()]
            high = [s for s in schools if 'high' in s.get('name', '').lower()]
            
            # Handle missing job data gracefully
            jobs_data = district_data.get('total_jobs', 0)
            jobs_info = f"• Current Job Postings: {jobs_data}" if jobs_data else "• Job posting data not available (district may not use Applitrack)"
            
            # Build prompt
            prompt = f"""You are a sales strategist preparing for a call with {district_name}. Using the data provided AND ONE targeted web search, create a comprehensive demo preparation script.

═══════════════════════════════════════════════════════════════
DISTRICT PROFILE: {district_name}
═══════════════════════════════════════════════════════════════

BASICS:
• Location: {district_data.get('county', 'Unknown')} County, {district_data.get('state', 'Unknown')}
• Total Enrollment: {district_data.get('enrollment', 0):,} students
• Number of Schools: {district_data.get('num_schools', 0)}
  - Elementary: {len(elementary)} schools
  - Middle: {len(middle)} schools  
  - High: {len(high)} schools
{jobs_info}

DEMOGRAPHICS:
• Free/Reduced Lunch: {demographics.get('free_reduced_lunch_pct', 'N/A')}%
• White Students: {demographics.get('white_pct', 'N/A')}%
• Minority Students: {demographics.get('minority_pct', 'N/A')}%

CLIENT STATUS:
• Target Client: {'Yes' if district_data.get('is_target_client') else 'No'}
• Radar Client: {'Yes' if district_data.get('is_radar_client') else 'No'}

SIMILAR DISTRICTS:
{chr(10).join([f"• {d.get('name', 'Unknown')}: {d.get('enrollment', 0):,} students, {d.get('num_schools', 0)} schools{', ' + str(d.get('total_jobs', 0)) + ' jobs' if d.get('total_jobs') else ''}" for d in similar_districts[:5]])}

═══════════════════════════════════════════════════════════════
WEB RESEARCH TASK (KEEP IT MINIMAL):
═══════════════════════════════════════════════════════════════

Do ONE search: "{district_name} {district_data.get('state', '')} school board minutes staffing recruiting 2024"

IMPORTANT: Only look at the TOP 2-3 results to stay efficient. Find:
1. Recent board meeting discussions about staffing/recruiting
2. News about workforce issues or teacher shortages
3. Mentions of recruitment strategies or external partners
4. Budget discussions related to personnel

If no relevant results found in top 2-3, use the data provided instead.
Include dates and source URLs for anything you find.

═══════════════════════════════════════════════════════════════
CREATE DEMO SCRIPT WITH THESE SECTIONS:
═══════════════════════════════════════════════════════════════

1. OPENING QUESTIONS (5-7 questions)
   Reference their data AND recent board/news findings if available.
   If no board minutes found, base questions on data alone.
   Example: "With {district_data.get('enrollment', 0):,} students and {demographics.get('free_reduced_lunch_pct', 'N/A')}% FRL, how is recruiting going?"

2. RECENT CONTEXT (2-3 insights)
   If board minutes/news found: Quote specific findings with dates and sources.
   If no board minutes found: Note "No recent public board discussions found. Focus on data-driven insights below."

3. KEY TALKING POINTS (4-5 points)
   Based on their demographics, size, and any recent context found.

4. PAIN POINTS TO PROBE (3-4 challenges)
   Based on data and typical challenges for districts of this profile.

5. COMPETITIVE CONTEXT (2-3 insights)
   Compare to similar districts using the data provided.

6. VALUE PROPOSITION ANGLE (1 paragraph)
   Tailored to their specific situation.

7. OBJECTION HANDLING (2-3 objections)
   Based on their profile (size, budget indicators, etc).

8. CLOSING RECOMMENDATIONS (2-3 strategies)
   Specific to this district's situation.

═══════════════════════════════════════════════════════════════
FORMATTING:
═══════════════════════════════════════════════════════════════

• Use specific numbers from data
• Include source URLs for any web findings
• Keep concise - total response under 2000 words
• Make it actionable for sales call

Begin:"""

            print(f"Generating demo script for {district_name} (with board search)")
            
            # ONE TARGETED WEB SEARCH - Just board minutes/news
            import time
            message = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=3500,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }],
                messages=[{"role": "user", "content": prompt}]
            )
            
            script = ""
            for block in message.content:
                if block.type == "text":
                    script += block.text + "\n"
            
            print(f"Demo script complete for {district_name}")
            
            # Delay to respect rate limits
            time.sleep(8)  # ~7 scripts/minute with search
            
            return {
                "district_name": district_name,
                "generated_at": datetime.now().isoformat(),
                "script_type": "demo_with_board_search",
                "basic_data": district_data,
                "similar_districts": similar_districts,
                "demo_script": script,
                "estimated_cost": 0.006,
                "generation_time": "~15 seconds",
                "has_job_data": bool(jobs_data)
            }
            
        except Exception as e:
            print(f"Error generating demo script: {str(e)}")
            import traceback
            traceback.print_exc()
            # Return detailed error info
            return {
                "error": True,
                "error_message": str(e),
                "district_name": district_name,
                "error_type": type(e).__name__
            }
    
    
    def _convert_objectids_to_strings(self, obj):
        """Recursively convert all ObjectIds to strings for JSON serialization"""
        from bson import ObjectId
        
        if isinstance(obj, ObjectId):
            return str(obj)
        elif isinstance(obj, dict):
            return {key: self._convert_objectids_to_strings(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_objectids_to_strings(item) for item in obj]
        elif isinstance(obj, datetime):
            return obj.isoformat()
        else:
            return obj
    
    def generate_school_hr_report(self, district_name):
        """
        Generate HR Administrator Report focused on job posting quality
        Queries the jobs collection where jobs have districtId reference
        """
        
        try:
            # Get district data
            district_data = self.get_district_basics(district_name)
            if not district_data:
                return {"error": "District not found"}
            
            from bson import ObjectId
            
            # Get the district document to get the ObjectId
            district_doc = self.db.districts.find_one(
                {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
            )
            
            if not district_doc:
                return {"error": "District not found in database"}
            
            district_id = district_doc.get('_id')
            
            if not district_id:
                return {"error": "Invalid district ID"}
            
            print(f"District: {district_name}")
            print(f"District ID: {district_id}")
            
            # Query the jobs collection - ONLY OPEN/ACTIVE JOBS
            all_jobs_raw = list(self.db.jobs.find({
                "districtId": district_id,
                "status": {"$ne": "closed"}  # Exclude closed jobs
            }))
            
            print(f"Found {len(all_jobs_raw)} OPEN jobs for {district_name}")
            
            if not all_jobs_raw:
                # Try alternative - maybe districtId is stored as string
                all_jobs_raw = list(self.db.jobs.find({
                    "districtId": str(district_id),
                    "status": {"$ne": "closed"}
                }))
                print(f"Found {len(all_jobs_raw)} OPEN jobs using string districtId")
                
                if not all_jobs_raw:
                    return {
                        "error": "No jobs found",
                        "message": f"No open jobs found for {district_name}. District may not have active job postings."
                    }
            
            # CRITICAL: Convert ObjectIds IMMEDIATELY after fetching from MongoDB
            print(f"Converting {len(all_jobs_raw)} jobs ObjectIds to strings...")
            all_jobs = self._convert_objectids_to_strings(all_jobs_raw)
            
            # Separate jobs with wage data vs without
            jobs_with_wages = [job for job in all_jobs if job.get('wage', {}).get('amount') or job.get('wage', {}).get('value')]
            jobs_without_wages = [job for job in all_jobs if not (job.get('wage', {}).get('amount') or job.get('wage', {}).get('value'))]
            
            print(f"Jobs with wage data: {len(jobs_with_wages)}")
            print(f"Jobs without wage data (unscraped): {len(jobs_without_wages)}")
            
            # Count jobs with classifications
            classified_count = sum(1 for job in all_jobs if job.get('aiClassification'))
            print(f"Jobs with aiClassification: {classified_count}/{len(all_jobs)}")
            
            # Analyze all jobs (for category breakdown and days open)
            print("Starting analysis...")
            analysis = self._analyze_jobs_for_hr(all_jobs, district_data)
            
            # Generate visualizations data
            print("Generating charts...")
            charts = self._generate_chart_data(all_jobs)
            
            # Generate quality report ONLY on jobs with wage data
            print("Generating quality report (jobs with wage data only)...")
            quality_report = self._generate_quality_report(jobs_with_wages)
            
            # Add unscraped jobs info
            quality_report['unscraped_jobs_count'] = len(jobs_without_wages)
            quality_report['unscraped_jobs'] = [
                {
                    "title": job.get('title', 'Unknown'),
                    "location": job.get('location', 'Unknown'),
                    "category": job.get('department') or job.get('positionType', 'Unclassified')
                }
                for job in jobs_without_wages[:10]  # Show first 10
            ]
            
            # Build result
            result = {
                "district_name": district_name,
                "generated_at": datetime.now().isoformat(),
                "report_type": "school_hr_admin",
                "total_jobs": len(all_jobs),
                "jobs_with_wage_data": len(jobs_with_wages),
                "jobs_without_wage_data": len(jobs_without_wages),
                "classified_jobs": classified_count,
                "analysis": analysis,
                "charts": charts,
                "quality_report": quality_report,
                "estimated_cost": 0.0
            }
            
            print("Converting result to JSON-safe format...")
            # Final conversion to ensure everything is serializable
            result = self._convert_objectids_to_strings(result)
            
            print("HR report generation complete")
            return result
            
        except Exception as e:
            print(f"Error generating school HR report: {str(e)}")
            import traceback
            traceback.print_exc()
            return {
                "error": True,
                "error_message": str(e),
                "error_type": type(e).__name__
            }
    
    def _analyze_jobs_for_hr(self, jobs, district_data):
        """Analyze jobs for HR administrator insights"""
        from collections import defaultdict
        
        # Group by department (using aiClassification, department, or positionType)
        by_category = defaultdict(list)
        for job in jobs:
            # Try multiple fields to get category
            ai_classification = job.get('aiClassification', {})
            if isinstance(ai_classification, dict):
                category = ai_classification.get('category')
            else:
                category = None
            
            # Fallback to department or positionType
            if not category:
                category = job.get('department') or job.get('positionType') or 'Unclassified'
            
            by_category[category].append(job)
        
        # Calculate metrics by category
        category_metrics = {}
        for category, cat_jobs in by_category.items():
            # Calculate average days open
            days_open = []
            for job in cat_jobs:
                posted = job.get('datePosted')
                if posted:
                    try:
                        if isinstance(posted, str):
                            posted_date = datetime.fromisoformat(posted.replace('Z', '+00:00'))
                        else:
                            posted_date = posted
                        
                        if posted_date.tzinfo:
                            days = (datetime.now(posted_date.tzinfo) - posted_date).days
                        else:
                            days = (datetime.now() - posted_date).days
                        days_open.append(days)
                    except:
                        pass
            
            avg_days = sum(days_open) / len(days_open) if days_open else 0
            
            category_metrics[category] = {
                "count": len(cat_jobs),
                "avg_days_open": round(avg_days, 1),
                "jobs": cat_jobs
            }
        
        return {
            "by_category": category_metrics,
            "total_categories": len(category_metrics)
        }
    
    def _generate_chart_data(self, jobs):
        """Generate data for pie chart visualization"""
        from collections import Counter
        
        # Count by category (from aiClassification, department, or positionType)
        categories = []
        for job in jobs:
            ai_classification = job.get('aiClassification', {})
            if isinstance(ai_classification, dict):
                category = ai_classification.get('category')
            else:
                category = None
            
            # Fallback to department or positionType
            if not category:
                category = job.get('department') or job.get('positionType') or 'Unclassified'
            
            categories.append(category)
        
        category_counts = Counter(categories)
        
        # Prepare pie chart data
        pie_data = {
            "labels": list(category_counts.keys()),
            "values": list(category_counts.values()),
            "colors": self._get_category_colors(list(category_counts.keys()))
        }
        
        return {
            "pie_chart": pie_data
        }
    
    def _get_category_colors(self, categories):
        """Assign colors to job categories"""
        color_map = {
            "Teacher": "#116753",
            "Support Staff": "#89BEF4",
            "Administrator": "#D776C2",
            "Specialist": "#FED46B",
            "Paraprofessional": "#E8F0CA",
            "Custodial": "#02223C",
            "Transportation": "#4A90E2",
            "Food Service": "#F39C12",
            "Athletics": "#E74C3C",
            "Unclassified": "#95A5A6"
        }
        return [color_map.get(cat, "#95A5A6") for cat in categories]
    
    def _compare_wages_to_nearby(self, district_data, jobs):
        """Compare wages to nearby districts"""
        
        # Get nearby districts
        nearby = list(self.db.districts.find({
            "$or": [
                {"county": district_data.get('county')},
                {"state": district_data.get('state')}
            ],
            "name": {"$ne": district_data['name']}
        }).limit(10))
        
        # Collect wages by type from this district
        district_wages = {
            "hourly": [],
            "salary": [],
            "stipend": []
        }
        
        for job in jobs:
            # Check wage field structure
            wage = job.get('wage', {})
            wage_type = str(wage.get('type', 'unknown')).lower()
            
            # Try amount from wage object first, then compensation
            amount = wage.get('amount') or wage.get('value')
            if not amount:
                compensation = job.get('compensation', {})
                amount = compensation.get('amount')
            
            if amount and isinstance(amount, (int, float)) and amount > 0:
                wage_info = {
                    "amount": amount,
                    "title": job.get('title', 'Unknown'),
                    "category": job.get('aiClassification', {}).get('category') or job.get('department', 'Unknown')
                }
                
                if 'hour' in wage_type:
                    district_wages['hourly'].append(wage_info)
                elif 'salary' in wage_type or 'annual' in wage_type:
                    district_wages['salary'].append(wage_info)
                elif 'stipend' in wage_type:
                    district_wages['stipend'].append(wage_info)
        
        return {
            "district_wages": district_wages,
            "nearby_districts": [d.get('name') for d in nearby],
            "comparison_available": False,
            "note": "Wage comparison requires nearby districts to have scraped jobs"
        }
    
    def _generate_quality_report(self, jobs):
        """Analyze job posting quality with specific callouts"""
        
        quality_issues = []
        top_jobs = []
        opportunities = []
        
        # Calculate average word count
        word_counts = []
        
        for job in jobs:
            job_score = 0
            issues = []
            reasons = []  # Track why job is good/bad
            
            title = job.get('title', '')
            description = job.get('fullDescription', '') or job.get('description', '')
            wage = job.get('wage', {})
            location = job.get('location', '')
            
            # Count words
            word_count = len(description.split())
            word_counts.append(word_count)
            
            # Check for spelling errors
            common_errors = [
                ('techer', 'teacher'), ('adminstrator', 'administrator'),
                ('assitant', 'assistant'), ('pricipal', 'principal'),
                ('secratary', 'secretary'), ('libraian', 'librarian')
            ]
            
            for wrong, right in common_errors:
                if wrong in description.lower() or wrong in title.lower():
                    issues.append(f"Spelling: '{wrong}' should be '{right}'")
                    reasons.append(f"❌ Contains spelling error: '{wrong}'")
                    job_score -= 10
            
            # Check for wage/salary information
            wage_amount = wage.get('amount') or wage.get('value')
            if not wage_amount:
                issues.append("Missing salary/wage information")
                reasons.append("❌ No salary/wage information provided")
                job_score -= 20
            else:
                reasons.append("✓ Includes salary/wage information")
                job_score += 20
            
            # Check for job description length
            if len(description) < 100:
                issues.append("Description too short (< 100 characters)")
                reasons.append(f"❌ Very short description ({len(description)} chars)")
                job_score -= 15
            elif len(description) > 200:
                reasons.append(f"✓ Comprehensive description ({word_count} words)")
                job_score += 15
            
            # Check for key information
            required_fields = ['qualifications', 'requirements', 'responsibilities']
            found_fields = []
            for field in required_fields:
                if field.lower() in description.lower():
                    job_score += 10
                    found_fields.append(field)
                else:
                    issues.append(f"Missing section: {field}")
            
            if found_fields:
                reasons.append(f"✓ Includes key sections: {', '.join(found_fields)}")
            if len(found_fields) < len(required_fields):
                missing = [f for f in required_fields if f not in found_fields]
                reasons.append(f"❌ Missing: {', '.join(missing)}")
            
            # Check for application deadline
            if job.get('closingDate'):
                job_score += 10
                reasons.append("✓ Has application deadline")
            else:
                issues.append("No application deadline specified")
                reasons.append("❌ No application deadline")
                job_score -= 5
            
            # Check for contact information
            if 'contact' in description.lower() or 'email' in description.lower():
                job_score += 5
                reasons.append("✓ Includes contact information")
            
            # Normalize score to 0-100
            job_score = max(0, min(100, 50 + job_score))
            
            job_analysis = {
                "title": title,
                "school": location or 'Location not specified',
                "category": job.get('aiClassification', {}).get('category') or job.get('department', 'Unclassified'),
                "quality_score": job_score,
                "issues": issues,
                "reasons": reasons,
                "posted_date": str(job.get('datePosted', 'Unknown')),
                "word_count": word_count,
                "full_description": description  # Include for expansion
            }
            
            # Categorize
            if job_score >= 80:
                top_jobs.append(job_analysis)
            elif job_score < 50:
                opportunities.append(job_analysis)
            
            if issues:
                quality_issues.append(job_analysis)
        
        # Calculate overall quality score
        all_scores = [job.get('quality_score', 0) for job in quality_issues + top_jobs + opportunities]
        overall_score = sum(all_scores) / len(all_scores) if all_scores else 0
        
        # Calculate average word count
        avg_word_count = sum(word_counts) / len(word_counts) if word_counts else 0
        
        return {
            "overall_quality_score": round(overall_score, 1),
            "total_jobs_analyzed": len(jobs),
            "avg_word_count": round(avg_word_count, 1),
            "top_performing_jobs": sorted(top_jobs, key=lambda x: x['quality_score'], reverse=True)[:10],
            "improvement_opportunities": sorted(opportunities, key=lambda x: x['quality_score'])[:10],
            "quality_issues": quality_issues
        }
    
    def generate_hr_report_pdf(self, report_data):
        """Generate PDF for HR report"""
        if not report_data:
            return None
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72
        )
        
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=HexColor('#116753'),
            spaceAfter=30,
            alignment=TA_CENTER,
            fontName='Helvetica-Bold'
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=HexColor('#116753'),
            spaceAfter=12,
            spaceBefore=20,
            fontName='Helvetica-Bold'
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=10,
            leading=14
        )
        
        story = []
        
        # Title
        story.append(Paragraph(f"HR Administrator Report", title_style))
        story.append(Paragraph(f"{report_data['district_name']}", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Metadata
        meta_text = f"<b>Generated:</b> {datetime.now().strftime('%B %d, %Y at %I:%M %p')}<br/>"
        meta_text += f"<b>Total Jobs Analyzed:</b> {report_data.get('total_jobs', 0)}"
        story.append(Paragraph(meta_text, normal_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Executive Summary
        story.append(Paragraph("Executive Summary", heading_style))
        quality = report_data.get('quality_report', {})
        analysis = report_data.get('analysis', {})
        
        summary_text = f"""
        This report analyzes {report_data.get('total_jobs', 0)} job postings across 
        {analysis.get('total_categories', 0)} categories. The overall quality score is 
        {quality.get('overall_quality_score', 0)}/100.
        """
        story.append(Paragraph(summary_text, normal_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Jobs by Category Table
        story.append(PageBreak())
        story.append(Paragraph("Jobs by Category", heading_style))
        
        by_category = analysis.get('by_category', {})
        if by_category:
            table_data = [['Category', 'Count', 'Avg Days Open', 'Avg Word Count']]
            
            for category, metrics in by_category.items():
                table_data.append([
                    category,
                    str(metrics.get('count', 0)),
                    str(metrics.get('avg_days_open', 0)),
                    str(int(metrics.get('avg_word_count', 0)))
                ])
            
            t = Table(table_data, colWidths=[2.5*inch, 1*inch, 1.2*inch, 1.3*inch])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), HexColor('#116753')),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 11),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), HexColor('#F0F0F0')),
                ('GRID', (0, 0), (-1, -1), 1, black),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 10),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor('#F9F9F9')]),
            ]))
            
            story.append(t)
            story.append(Spacer(1, 0.3*inch))
        
        # Top Performing Jobs
        story.append(PageBreak())
        story.append(Paragraph("Top Performing Jobs", heading_style))
        
        top_jobs = quality.get('top_performing_jobs', [])
        if top_jobs:
            for i, job in enumerate(top_jobs, 1):
                job_text = f"<b>{i}. {job['title']}</b><br/>"
                job_text += f"Category: {job['category']}<br/>"
                job_text += f"Location: {job['school']}<br/>"
                job_text += f"Quality Score: {job['quality_score']}/100"
                story.append(Paragraph(job_text, normal_style))
                story.append(Spacer(1, 0.15*inch))
        
        # Improvement Opportunities
        story.append(PageBreak())
        story.append(Paragraph("Improvement Opportunities", heading_style))
        
        opportunities = quality.get('improvement_opportunities', [])
        if opportunities:
            for i, job in enumerate(opportunities, 1):
                job_text = f"<b>{i}. {job['title']}</b><br/>"
                job_text += f"Category: {job['category']}<br/>"
                job_text += f"Location: {job['school']}<br/>"
                job_text += f"Quality Score: {job['quality_score']}/100<br/>"
                if job.get('issues'):
                    job_text += f"Issues: {', '.join(job['issues'])}"
                story.append(Paragraph(job_text, normal_style))
                story.append(Spacer(1, 0.15*inch))
        
        # Build PDF
        doc.build(story)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        return pdf_data
    
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

@app.route('/api/generate-demo-script', methods=['POST'])
def generate_demo_script_endpoint():
    """Generate demo script using MongoDB data + board minutes search"""
    data = request.json
    district_name = data.get('district_name')
    
    if not district_name:
        return jsonify({"error": "District name required"}), 400
    
    try:
        print(f"Generating demo script for {district_name}")
        result = generator.generate_demo_script(district_name)
        
        if not result:
            return jsonify({"error": "District not found"}), 404
        
        # Check if result contains an error
        if isinstance(result, dict) and result.get('error'):
            error_msg = result.get('error_message', 'Unknown error')
            error_type = result.get('error_type', 'Error')
            print(f"Demo script error: {error_type}: {error_msg}")
            return jsonify({
                "error": f"{error_type}: {error_msg}",
                "district_name": district_name
            }), 500
        
        print(f"Demo script complete for {district_name}")
        
        return jsonify({
            "success": True,
            "script": result
        })
    
    except Exception as e:
        print(f"Error generating demo script: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500

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

@app.route('/api/generate-hr-report', methods=['POST'])
def generate_hr_report_endpoint():
    """Generate HR Administrator Report for job posting analysis"""
    data = request.json
    district_name = data.get('district_name')
    
    if not district_name:
        return jsonify({"error": "District name required"}), 400
    
    try:
        print(f"Generating HR report for {district_name}")
        result = generator.generate_school_hr_report(district_name)
        
        if result.get('error'):
            return jsonify({
                "error": result.get('error'),
                "message": result.get('message', result.get('error_message', 'Unknown error'))
            }), 404 if result.get('error') == 'District not found' else 500
        
        print(f"HR report complete for {district_name}")
        
        return jsonify({
            "success": True,
            "report": result
        })
    
    except Exception as e:
        print(f"Error generating HR report: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500

@app.route('/api/download-hr-report-pdf', methods=['POST'])
def download_hr_report_pdf():
    """Generate and download HR report as PDF"""
    data = request.json
    report_data = data.get('report_data')
    
    if not report_data:
        return jsonify({"error": "Report data required"}), 400
    
    try:
        print(f"Generating HR report PDF for {report_data.get('district_name', 'Unknown')}")
        pdf_data = generator.generate_hr_report_pdf(report_data)
        
        if not pdf_data:
            return jsonify({"error": "PDF generation failed"}), 500
        
        # Create filename
        district_name = report_data.get('district_name', 'Unknown').replace(' ', '_')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"HR_Report_{district_name}_{timestamp}.pdf"
        
        return Response(
            pdf_data,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment;filename={filename}'}
        )
    
    except Exception as e:
        print(f"Error generating HR report PDF: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500