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
import time

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
- Any standout differences

PART 4 - Financial Health Indicators:
Search for recent news about {district_name} {state} related to:
- Budget issues or financial difficulties
- Enrollment trends (growing/declining)
- Recent bond votes or mill levies
- Teacher retention challenges
- Any positive financial indicators

Respond in this EXACT format:

CAREER PAGE:
[URL]
[findings]

INDEED PRESENCE:
[findings]

COMPETITIVE COMPARISON:
[findings]

FINANCIAL HEALTH:
[findings]

INSIGHTS:
[your analysis of what this all means]
"""
        
        print(f"Searching web for {district_name} job data...")
        
        response = self.anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": prompt
            }]
        )
        
        # Extract text from response
        analysis = ""
        for block in response.content:
            if hasattr(block, 'text'):
                analysis += block.text
        
        return analysis
    
    def generate_report(self, district_name, contact_names=None):
        """
        Generate full district report with basic info, jobs, and demo script
        """
        print(f"Starting full report for {district_name}")
        
        # Get district basics
        district_data = self.get_district_basics(district_name)
        if not district_data:
            print(f"District not found: {district_name}")
            return None
        
        print(f"District data retrieved: {district_name}")
        
        # Find similar districts
        similar_districts = self.find_similar_districts(district_data)
        print(f"Similar districts found: {len(similar_districts)}")
        
        # Get jobs from MongoDB
        from bson import ObjectId
        district_doc = self.db.districts.find_one(
            {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
        )
        jobs_data = None
        if district_doc:
            district_id = district_doc.get('_id')
            all_jobs = list(self.db.jobs.find({"districtId": district_id}))
            if all_jobs:
                jobs_data = {
                    "total_jobs": len(all_jobs),
                    "jobs": all_jobs
                }
                print(f"Found {len(all_jobs)} jobs in database")
        
        # Scrape with Indeed analysis
        jobs_analysis = self.scrape_job_website_with_indeed(
            district_name,
            district_data['state'],
            similar_districts
        )
        print(f"Web scraping complete")
        
        # Generate demo script
        demo_script = self.generate_demo_script(district_name, contact_names)
        print(f"Demo script generated")
        
        return {
            "district_name": district_name,
            "generated_at": datetime.now().isoformat(),
            "basic_data": district_data,
            "similar_districts": similar_districts,
            "jobs_analysis": jobs_analysis,
            "demo_script": demo_script,
            "jobs_data": jobs_data,
            "estimated_cost": 0.015
        }
    
    def generate_demo_script(self, district_name, contact_names=None):
        """Generate demo meeting script with board minutes context"""
        
        try:
            # Get district basics
            district_data = self.get_district_basics(district_name)
            if not district_data:
                return {"error": "District not found"}
            
            # Find similar districts
            similar_districts = self.find_similar_districts(district_data)
            
            # Get jobs from MongoDB
            from bson import ObjectId
            district_doc = self.db.districts.find_one(
                {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
            )
            
            jobs_data = None
            if district_doc:
                district_id = district_doc.get('_id')
                all_jobs = list(self.db.jobs.find({"districtId": district_id}))
                if all_jobs:
                    jobs_data = {
                        "total_jobs": len(all_jobs),
                        "sample_titles": [job.get('title', 'Untitled') for job in all_jobs[:10]]
                    }
            
            # Build context for Claude
            context = f"""District: {district_data['name']}, {district_data['state']}
Enrollment: {district_data.get('enrollment', 'Unknown')}
Schools: {district_data.get('num_schools', 'Unknown')}
Open Jobs: {jobs_data['total_jobs'] if jobs_data else 'None in database'}
"""
            
            if district_data.get('demographics'):
                demo = district_data['demographics']
                context += f"\nDemographics:"
                if demo.get('free_reduced_lunch_pct'):
                    context += f"\n- Free/Reduced Lunch: {demo['free_reduced_lunch_pct']}%"
                if demo.get('minority_pct'):
                    context += f"\n- Minority Students: {demo['minority_pct']}%"
            
            if jobs_data:
                context += f"\n\nSample Job Titles:\n"
                for title in jobs_data['sample_titles'][:5]:
                    context += f"- {title}\n"
            
            # Search for board minutes and recent news
            board_search_prompt = f"""Search for recent board meeting minutes and news for {district_data['name']} {district_data['state']}.

Find:
1. Recent board meeting minutes (last 3-6 months) - look for official district website or BoardDocs
2. Recent news articles about the district
3. Any public discussions about technology, HR challenges, recruitment, or operational improvements

Provide a summary of key themes, initiatives, challenges, or priorities that would be relevant for a demo meeting about recruitment and HR technology."""

            print(f"Searching for board minutes and news...")
            board_response = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{
                    "role": "user",
                    "content": board_search_prompt
                }]
            )
            
            # Extract board context
            board_context = ""
            for block in board_response.content:
                if hasattr(block, 'text'):
                    board_context += block.text
            
            # Generate the demo script
            contact_info = ""
            if contact_names:
                contact_info = f"\nKey contacts: {', '.join(contact_names)}"
            
            script_prompt = f"""Create a professional demo meeting script for presenting GoToro's applicant tracking system to {district_data['name']}.

DISTRICT CONTEXT:
{context}{contact_info}

BOARD MINUTES & RECENT NEWS:
{board_context}

Create a demo script that:
1. Opens with a personalized acknowledgment of their specific situation (based on board minutes/news if relevant)
2. Addresses their likely pain points (based on open jobs, demographics, recent challenges)
3. Highlights relevant GoToro features
4. Includes 2-3 specific questions to ask them
5. Suggests a strong closing ask

Format as a natural script with clear sections. Be conversational but professional."""

            script_response = self.anthropic.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=3000,
                messages=[{
                    "role": "user",
                    "content": script_prompt
                }]
            )
            
            script = ""
            for block in script_response.content:
                if hasattr(block, 'text'):
                    script += block.text
            
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
            
            # Query the jobs collection directly
            all_jobs = list(self.db.jobs.find({"districtId": district_id}))
            
            print(f"Found {len(all_jobs)} jobs for {district_name}")
            
            if not all_jobs:
                # Try alternative - maybe districtId is stored as string
                all_jobs_str = list(self.db.jobs.find({"districtId": str(district_id)}))
                print(f"Found {len(all_jobs_str)} jobs using string districtId")
                
                if all_jobs_str:
                    all_jobs = all_jobs_str
                else:
                    return {
                        "error": "No jobs found",
                        "message": f"No jobs found for {district_name}. District may not have scraped jobs yet."
                    }
            
            # CRITICAL: Convert ObjectIds IMMEDIATELY after fetching from MongoDB
            print(f"Converting {len(all_jobs)} jobs ObjectIds to strings...")
            all_jobs = self._convert_objectids_to_strings(all_jobs)
            
            # Count jobs with classifications
            classified_count = sum(1 for job in all_jobs if job.get('aiClassification'))
            print(f"Jobs with aiClassification: {classified_count}/{len(all_jobs)}")
            
            # Analyze jobs
            print("Starting analysis...")
            analysis = self._analyze_jobs_for_hr(all_jobs, district_data)
            
            # Generate visualizations data
            print("Generating charts...")
            charts = self._generate_chart_data(all_jobs)
            
            # Generate quality report (without full descriptions for API response)
            print("Generating quality report...")
            quality_report = self._generate_quality_report(all_jobs, include_full_descriptions=False)
            
            # Build result
            result = {
                "district_name": district_name,
                "generated_at": datetime.now().isoformat(),
                "report_type": "school_hr_admin",
                "total_jobs": len(all_jobs),
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
        """Analyze jobs for HR administrator insights with AVERAGE WORD COUNT"""
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
            
            # Calculate average word count for descriptions
            word_counts = []
            for job in cat_jobs:
                description = job.get('fullDescription', '') or job.get('description', '')
                if description:
                    # Count words (split by whitespace)
                    words = len(description.split())
                    word_counts.append(words)
            
            avg_word_count = sum(word_counts) / len(word_counts) if word_counts else 0
            
            category_metrics[category] = {
                "count": len(cat_jobs),
                "avg_days_open": round(avg_days, 1),
                "avg_word_count": round(avg_word_count, 0),
                "jobs": cat_jobs
            }
        
        # Sort category_metrics by avg_days_open (highest to lowest)
        sorted_categories = dict(sorted(
            category_metrics.items(),
            key=lambda x: x[1]['avg_days_open'],
            reverse=True
        ))
        
        return {
            "by_category": sorted_categories,
            "total_categories": len(category_metrics)
        }
    
    def _generate_chart_data(self, jobs):
        """Generate data for pie chart visualization - sorted by count"""
        from collections import Counter
        
        # Count by category (from aiClassification, department, or positionType)
        categories = []
        locations = []
        
        for job in jobs:
            # Category
            ai_classification = job.get('aiClassification', {})
            if isinstance(ai_classification, dict):
                category = ai_classification.get('category')
            else:
                category = None
            
            # Fallback to department or positionType
            if not category:
                category = job.get('department') or job.get('positionType') or 'Unclassified'
            
            categories.append(category)
            
            # Location
            location = job.get('location', 'Unknown Location')
            if not location:
                location = 'Unknown Location'
            locations.append(location)
        
        category_counts = Counter(categories)
        location_counts = Counter(locations)
        
        # Sort by count (highest to lowest)
        sorted_categories = category_counts.most_common()
        sorted_locations = location_counts.most_common()
        
        # Prepare pie chart data (sorted)
        category_pie_data = {
            "labels": [item[0] for item in sorted_categories],
            "values": [item[1] for item in sorted_categories],
            "colors": self._get_category_colors([item[0] for item in sorted_categories])
        }
        
        location_pie_data = {
            "labels": [item[0] for item in sorted_locations],
            "values": [item[1] for item in sorted_locations],
            "colors": self._get_location_colors([item[0] for item in sorted_locations])
        }
        
        return {
            "category_pie_chart": category_pie_data,
            "location_pie_chart": location_pie_data
        }
    
    def _get_category_colors(self, categories):
        """Assign colors to job categories with better variety"""
        color_map = {
            "Teacher": "#2C5F2D",
            "Support Staff": "#4A90E2",
            "Administrator": "#8B5CF6",
            "Specialist": "#F59E0B",
            "Paraprofessional": "#10B981",
            "Custodial": "#6366F1",
            "Transportation": "#EC4899",
            "Food Service": "#F97316",
            "Athletics": "#EF4444",
            "Unclassified": "#94A3B8"
        }
        
        # Generate additional colors if needed
        extra_colors = ["#06B6D4", "#84CC16", "#A855F7", "#F43F5E", "#14B8A6", 
                       "#FB923C", "#3B82F6", "#22C55E", "#A78BFA", "#FCD34D"]
        
        result = []
        for i, cat in enumerate(categories):
            if cat in color_map:
                result.append(color_map[cat])
            else:
                # Use extra colors cycling
                result.append(extra_colors[i % len(extra_colors)])
        
        return result
    
    def _get_location_colors(self, locations):
        """Assign colors to locations with good variety"""
        # Use a different color palette for locations
        colors = [
            "#0EA5E9", "#8B5CF6", "#EC4899", "#F59E0B", "#10B981",
            "#6366F1", "#F97316", "#14B8A6", "#A855F7", "#84CC16",
            "#EF4444", "#06B6D4", "#FB923C", "#3B82F6", "#22C55E"
        ]
        
        return [colors[i % len(colors)] for i in range(len(locations))]
    
    def _generate_quality_report(self, jobs, include_full_descriptions=False):
        """Analyze job posting quality with specific callouts and explanations"""
        
        quality_issues = []
        top_jobs = []
        opportunities = []
        
        for job in jobs:
            job_score = 0
            issues = []
            strengths = []
            
            title = job.get('title', '')
            description = job.get('fullDescription', '') or job.get('description', '')
            wage = job.get('wage', {})
            location = job.get('location', '')
            
            # Check for spelling errors
            common_errors = [
                ('techer', 'teacher'), ('adminstrator', 'administrator'),
                ('assitant', 'assistant'), ('pricipal', 'principal'),
                ('secratary', 'secretary'), ('libraian', 'librarian')
            ]
            
            for wrong, right in common_errors:
                if wrong in description.lower() or wrong in title.lower():
                    issues.append(f"Spelling: '{wrong}' should be '{right}'")
                    job_score -= 10
            
            # Check for wage/salary information
            wage_amount = wage.get('amount') or wage.get('value')
            if not wage_amount:
                issues.append("Missing salary/wage information")
                job_score -= 20
            else:
                job_score += 20
                strengths.append("Includes salary information")
            
            # Check for job description length
            word_count = len(description.split())
            if len(description) < 100:
                issues.append("Description too short (< 100 characters)")
                job_score -= 15
            elif word_count > 150:
                job_score += 15
                strengths.append(f"Comprehensive description ({word_count} words)")
            
            # Check for key information
            required_fields = ['qualifications', 'requirements', 'responsibilities']
            for field in required_fields:
                if field.lower() in description.lower():
                    job_score += 10
                    strengths.append(f"Includes {field}")
                else:
                    issues.append(f"Missing section: {field}")
                    job_score -= 5
            
            # Check for application deadline
            if job.get('closingDate'):
                job_score += 10
                strengths.append("Has application deadline")
            else:
                issues.append("No application deadline specified")
                job_score -= 5
            
            # Check for contact information
            if 'contact' in description.lower() or 'email' in description.lower():
                job_score += 5
                strengths.append("Includes contact information")
            
            # Normalize score to 0-100
            job_score = max(0, min(100, 50 + job_score))
            
            job_analysis = {
                "title": title,
                "school": location or 'Location not specified',
                "category": job.get('aiClassification', {}).get('category') or job.get('department', 'Unclassified'),
                "quality_score": job_score,
                "issues": issues,
                "strengths": strengths,
                "posted_date": str(job.get('datePosted', 'Unknown')),
                "word_count": word_count,
                "job_id": str(job.get('_id', ''))  # Store job ID for later retrieval
            }
            
            # Only include full description if requested (for PDF generation)
            if include_full_descriptions:
                job_analysis["full_description"] = description
            else:
                # Include just a preview
                job_analysis["description_preview"] = description[:200] + "..." if len(description) > 200 else description
            
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
        
        return {
            "overall_quality_score": round(overall_score, 1),
            "total_jobs_analyzed": len(jobs),
            "jobs_with_issues": len(quality_issues),
            "top_performing_jobs": sorted(top_jobs, key=lambda x: x['quality_score'], reverse=True)[:5],
            "improvement_opportunities": sorted(opportunities, key=lambda x: x['quality_score'])[:10],
            "quality_issues": quality_issues
        }
    
    def generate_pdf(self, report):
        """Generate PDF with improved formatting"""
        if not report:
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
        
        # Get styles
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=HexColor('#116753'),
            spaceAfter=30,
            alignment=TA_CENTER
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=14,
            textColor=HexColor('#116753'),
            spaceAfter=12,
            spaceBefore=12
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=10,
            leading=14
        )
        
        # Build content
        story = []
        
        # Title
        story.append(Paragraph(f"District Report: {report['district_name']}", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Basic Info
        basic = report.get('basic_data', {})
        story.append(Paragraph("District Information", heading_style))
        
        info_text = f"""
        <b>State:</b> {basic.get('state', 'N/A')}<br/>
        <b>County:</b> {basic.get('county', 'N/A')}<br/>
        <b>Enrollment:</b> {basic.get('enrollment', 'N/A'):,}<br/>
        <b>Number of Schools:</b> {basic.get('num_schools', 'N/A')}<br/>
        <b>Total Jobs:</b> {basic.get('total_jobs', 'N/A')}
        """
        story.append(Paragraph(info_text, normal_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Demographics
        if basic.get('demographics'):
            demo = basic['demographics']
            story.append(Paragraph("Demographics", heading_style))
            demo_text = ""
            if demo.get('free_reduced_lunch_pct'):
                demo_text += f"<b>Free/Reduced Lunch:</b> {demo['free_reduced_lunch_pct']}%<br/>"
            if demo.get('minority_pct'):
                demo_text += f"<b>Minority Students:</b> {demo['minority_pct']}%<br/>"
            if demo_text:
                story.append(Paragraph(demo_text, normal_style))
                story.append(Spacer(1, 0.3*inch))
        
        # Similar Districts
        if report.get('similar_districts'):
            story.append(PageBreak())
            story.append(Paragraph("Similar Districts", heading_style))
            for district in report['similar_districts']:
                similar_text = f"""
                <b>{district['name']}</b><br/>
                Enrollment: {district.get('enrollment', 'N/A'):,} | 
                Schools: {district.get('num_schools', 'N/A')} | 
                County: {district.get('county', 'N/A')}
                """
                story.append(Paragraph(similar_text, normal_style))
                story.append(Spacer(1, 0.1*inch))
        
        # Jobs Analysis
        if report.get('jobs_analysis'):
            story.append(PageBreak())
            story.append(Paragraph("Job Posting Analysis", heading_style))
            
            # Clean the analysis text for PDF
            analysis = report['jobs_analysis']
            # Remove any problematic characters
            analysis = analysis.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            # Split into paragraphs
            for para in analysis.split('\n\n'):
                if para.strip():
                    story.append(Paragraph(para.strip(), normal_style))
                    story.append(Spacer(1, 0.1*inch))
        
        # Demo Script
        if report.get('demo_script'):
            if isinstance(report['demo_script'], dict):
                demo = report['demo_script'].get('demo_script', '')
            else:
                demo = report['demo_script']
            
            if demo:
                story.append(PageBreak())
                story.append(Paragraph("Demo Meeting Script", heading_style))
                
                # Clean the script text
                demo = str(demo).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                # Split into paragraphs
                for para in demo.split('\n\n'):
                    if para.strip():
                        story.append(Paragraph(para.strip(), normal_style))
                        story.append(Spacer(1, 0.1*inch))
        
        # Build PDF
        doc.build(story)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        return pdf_data
    
    def save_report_to_db(self, report, pdf_data):
        """Save generated report and PDF to MongoDB"""
        if not report or not pdf_data:
            return None
        
        # Create filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{report['district_name'].replace(' ', '_')}_{timestamp}.pdf"
        
        # Store in MongoDB
        self.db.generated_reports.insert_one({
            "filename": filename,
            "district_name": report['district_name'],
            "generated_at": datetime.now(),
            "report_json": report,
            "pdf_data": base64.b64encode(pdf_data).decode('utf-8')
        })
        
        return filename
    
    def get_all_reports(self):
        """Retrieve list of all generated reports"""
        reports = list(self.db.generated_reports.find(
            {},
            {"filename": 1, "district_name": 1, "generated_at": 1, "_id": 0}
        ).sort("generated_at", -1))
        
        # Convert datetime to ISO format
        for report in reports:
            if 'generated_at' in report:
                report['generated_at'] = report['generated_at'].isoformat()
        
        return reports
    
    def get_report_pdf(self, filename):
        """Retrieve PDF data from MongoDB"""
        report = self.db.generated_reports.find_one({"filename": filename})
        
        if not report:
            return None
        
        return base64.b64decode(report["pdf_data"])
    
    def generate_hr_report_pdf(self, report_data):
        """Generate PDF for HR report with enhanced formatting"""
        if not report_data:
            return None
        
        # Fetch jobs with full descriptions for PDF
        try:
            from bson import ObjectId
            district_name = report_data.get('district_name')
            district_doc = self.db.districts.find_one(
                {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
            )
            
            if district_doc:
                district_id = district_doc.get('_id')
                all_jobs = list(self.db.jobs.find({"districtId": district_id}))
                all_jobs = self._convert_objectids_to_strings(all_jobs)
                
                # Regenerate quality report with full descriptions
                quality_report_full = self._generate_quality_report(all_jobs, include_full_descriptions=True)
                # Replace the quality report in report_data with the full version
                report_data['quality_report'] = quality_report_full
        except Exception as e:
            print(f"Warning: Could not fetch full job descriptions for PDF: {str(e)}")
            # Continue with preview descriptions if fetch fails
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72
        )
        
        # Get styles
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
        
        subheading_style = ParagraphStyle(
            'CustomSubHeading',
            parent=styles['Heading3'],
            fontSize=12,
            textColor=HexColor('#2C5F2D'),
            spaceAfter=8,
            spaceBefore=12,
            fontName='Helvetica-Bold'
        )
        
        normal_style = ParagraphStyle(
            'CustomNormal',
            parent=styles['Normal'],
            fontSize=10,
            leading=14,
            alignment=TA_JUSTIFY
        )
        
        bullet_style = ParagraphStyle(
            'CustomBullet',
            parent=styles['Normal'],
            fontSize=10,
            leading=14,
            leftIndent=20,
            bulletIndent=10
        )
        
        # Build content
        story = []
        
        # Title
        story.append(Paragraph(f"HR Administrator Report", title_style))
        story.append(Paragraph(f"{report_data['district_name']}", title_style))
        story.append(Spacer(1, 0.2*inch))
        
        # Report metadata
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
        {quality.get('jobs_with_issues', 0)} jobs have identified improvement opportunities, while 
        {len(quality.get('top_performing_jobs', []))} jobs demonstrate best practices.
        """
        story.append(Paragraph(summary_text, normal_style))
        story.append(Spacer(1, 0.3*inch))
        
        # Jobs by Category with Average Days Open and Word Count
        story.append(PageBreak())
        story.append(Paragraph("Jobs by Category", heading_style))
        
        by_category = analysis.get('by_category', {})
        if by_category:
            # Create table data
            table_data = [['Category', 'Count', 'Avg Days Open', 'Avg Word Count']]
            
            for category, metrics in by_category.items():
                table_data.append([
                    category,
                    str(metrics.get('count', 0)),
                    str(metrics.get('avg_days_open', 0)),
                    str(int(metrics.get('avg_word_count', 0)))
                ])
            
            # Create table
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
            story.append(Paragraph(
                f"These {len(top_jobs)} jobs demonstrate best practices in job posting quality:",
                normal_style
            ))
            story.append(Spacer(1, 0.15*inch))
            
            for i, job in enumerate(top_jobs, 1):
                story.append(Paragraph(f"<b>{i}. {job['title']}</b>", subheading_style))
                
                job_info = f"""
                <b>Category:</b> {job['category']}<br/>
                <b>Location:</b> {job['school']}<br/>
                <b>Quality Score:</b> {job['quality_score']}/100<br/>
                <b>Word Count:</b> {job.get('word_count', 'N/A')} words
                """
                story.append(Paragraph(job_info, normal_style))
                
                # Why it's top performing
                if job.get('strengths'):
                    story.append(Paragraph("<b>Why This Job Performs Well:</b>", normal_style))
                    for strength in job['strengths']:
                        story.append(Paragraph(f"• {strength}", bullet_style))
                
                # Job description (truncated if too long)
                description = job.get('full_description', '')
                if len(description) > 500:
                    description = description[:500] + "..."
                
                story.append(Paragraph("<b>Description Preview:</b>", normal_style))
                story.append(Paragraph(description.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'), 
                                     normal_style))
                story.append(Spacer(1, 0.2*inch))
        
        # Improvement Opportunities
        story.append(PageBreak())
        story.append(Paragraph("Improvement Opportunities", heading_style))
        
        opportunities = quality.get('improvement_opportunities', [])
        if opportunities:
            story.append(Paragraph(
                f"These {len(opportunities)} jobs have the most room for improvement:",
                normal_style
            ))
            story.append(Spacer(1, 0.15*inch))
            
            for i, job in enumerate(opportunities, 1):
                story.append(Paragraph(f"<b>{i}. {job['title']}</b>", subheading_style))
                
                job_info = f"""
                <b>Category:</b> {job['category']}<br/>
                <b>Location:</b> {job['school']}<br/>
                <b>Quality Score:</b> {job['quality_score']}/100<br/>
                <b>Word Count:</b> {job.get('word_count', 'N/A')} words
                """
                story.append(Paragraph(job_info, normal_style))
                
                # Issues
                if job.get('issues'):
                    story.append(Paragraph("<b>Identified Issues:</b>", normal_style))
                    for issue in job['issues']:
                        story.append(Paragraph(f"• {issue}", bullet_style))
                
                # Job description (truncated if too long)
                description = job.get('full_description', '')
                if len(description) > 500:
                    description = description[:500] + "..."
                
                story.append(Paragraph("<b>Description Preview:</b>", normal_style))
                story.append(Paragraph(description.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'), 
                                     normal_style))
                story.append(Spacer(1, 0.2*inch))
        
        # Recommendations
        story.append(PageBreak())
        story.append(Paragraph("Recommendations", heading_style))
        
        recommendations = f"""
        Based on the analysis of {report_data.get('total_jobs', 0)} job postings, here are key recommendations:
        """
        story.append(Paragraph(recommendations, normal_style))
        story.append(Spacer(1, 0.15*inch))
        
        recs = [
            "Ensure all job postings include salary/wage information to improve transparency",
            "Aim for job descriptions between 150-400 words for optimal comprehensiveness",
            "Include clear sections for qualifications, requirements, and responsibilities",
            "Add application deadlines to create urgency and improve candidate quality",
            "Proofread all postings to eliminate spelling and grammatical errors",
            "Include contact information to make the application process more personal"
        ]
        
        for rec in recs:
            story.append(Paragraph(f"• {rec}", bullet_style))
        
        # Build PDF
        doc.build(story)
        pdf_data = buffer.getvalue()
        buffer.close()
        
        return pdf_data

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

@app.route('/api/get-job-description/<district_name>/<job_id>')
def get_job_description(district_name, job_id):
    """Get full job description for a specific job"""
    try:
        from bson import ObjectId
        
        # Find the district
        district_doc = generator.db.districts.find_one(
            {"name": {"$regex": f"^{re.escape(district_name)}$", "$options": "i"}}
        )
        
        if not district_doc:
            return jsonify({"error": "District not found"}), 404
        
        # Find the job
        try:
            job = generator.db.jobs.find_one({"_id": ObjectId(job_id)})
        except:
            # Try as string if ObjectId conversion fails
            job = generator.db.jobs.find_one({"_id": job_id})
        
        if not job:
            return jsonify({"error": "Job not found"}), 404
        
        # Return full description
        description = job.get('fullDescription', '') or job.get('description', '')
        
        return jsonify({
            "success": True,
            "job_id": job_id,
            "full_description": description
        })
    
    except Exception as e:
        print(f"Error fetching job description: {str(e)}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)