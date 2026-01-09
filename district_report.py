#!/usr/bin/env python3
"""
District Report Generator
Combines MongoDB data with Claude API to create comprehensive district reports
"""

import os
import json
from datetime import datetime
from pymongo import MongoClient
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

class DistrictReportGenerator:
    def __init__(self, mongodb_uri, anthropic_api_key):
        """Initialize with MongoDB and Anthropic connections"""
        self.mongo_client = MongoClient(mongodb_uri)
        self.db = self.mongo_client['applitrack-job-scraper']  # Your database name
        self.anthropic = Anthropic(api_key=anthropic_api_key)
        
    def get_district_basics(self, district_name):
        """Retrieve basic district info from MongoDB districts collection"""
        # Search in the districts collection
        district = self.db.districts.find_one(
            {"name": {"$regex": district_name, "$options": "i"}}
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
        # Priority 1: Same county
        similar = list(self.db.districts.find({
            "state": state,
            "county": county,
            "name": {"$ne": district_data["name"]},
            "totalEnrollment": {
                "$gte": enrollment * 0.5,
                "$lte": enrollment * 1.5
            }
        }).limit(limit))
        
        # If not enough in same county, expand to same state
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
        print("🔍 Searching for job postings on district website...")
        
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
            
            # Extract text from response
            job_info = ""
            for block in message.content:
                if block.type == "text":
                    job_info += block.text + "\n"
            
            return job_info.strip()
        
        except Exception as e:
            print(f"⚠️  Could not scrape job website: {e}")
            return "Unable to retrieve current job postings from website."
    
    def analyze_with_claude(self, district_name, district_data, similar_districts, job_scrape_info):
        """Use Claude API to analyze board meetings and generate report"""
        
        # Prepare the prompt for Claude
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

Format this as a professional brief that would help our sales team prepare for outreach.

Please format your response as a structured report with clear sections."""

        # Call Claude API
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
        
        # Extract text from response
        analysis = ""
        for block in message.content:
            if block.type == "text":
                analysis += block.text + "\n"
        
        return analysis.strip()
    
  √
    def save_report(self, report, output_dir="reports"):
        """Save report to file"""
        if not report:
            return None
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Create filename from district name and timestamp
        district_name = report["district_name"].replace(" ", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{output_dir}/{district_name}_{timestamp}.json"
        
        with open(filename, 'w') as f:
            json.dump(report, f, indent=2)
        
        print(f"✅ Report saved to: {filename}")
        return filename
    
    def print_report(self, report):
        """Print report to console"""
        if not report:
            return
        
        print("\n" + "="*60)
        print(f"DISTRICT REPORT: {report['district_name']}")
        print("="*60)
        
        print("\n📋 BASIC INFORMATION")
        print("-" * 60)
        data = report['basic_data']
        print(f"LEA ID: {data['leaId']}")
        print(f"Enrollment: {data['enrollment']:,}")
        print(f"Number of Schools: {data['num_schools']}")
        print(f"Job Postings in Database: {data['total_jobs']}")
        print(f"Location: {data['county']}, {data['state']}")
        print(f"Target Client: {'Yes' if data['is_target_client'] else 'No'}")
        print(f"Radar Client: {'Yes' if data['is_radar_client'] else 'No'}")
        
        print("\n🔗 SIMILAR DISTRICTS")
        print("-" * 60)
        for district in report['similar_districts']:
            print(f"• {district['name']} ({district['county']})")
            print(f"  Enrollment: {district['enrollment']:,}, Schools: {district['num_schools']}, Jobs: {district['total_jobs']}")
        
        print("\n💼 CURRENT JOB POSTINGS (from website)")
        print("-" * 60)
        print(report['job_scrape_info'])
        
        print("\n🤖 CLAUDE ANALYSIS")
        print("-" * 60)
        print(report['claude_analysis'])
        print("\n" + "="*60)


def main():
    load_dotenv()
    # Load configuration from environment variables
    mongodb_uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    
    if not anthropic_api_key:
        print("❌ Error: ANTHROPIC_API_KEY environment variable not set")
        print("Please set it with: export ANTHROPIC_API_KEY='your-api-key'")
        return
    
    # Get district name from user
    district_name = input("\nEnter district name to research: ").strip()
    
    if not district_name:
        print("❌ District name required")
        return
    
    # Generate report
    generator = DistrictReportGenerator(mongodb_uri, anthropic_api_key)
    report = generator.generate_report(district_name)
    
    if report:
        generator.print_report(report)
        generator.save_report(report)


if __name__ == "__main__":
    main()