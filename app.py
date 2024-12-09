from flask import Flask, render_template, request, flash, redirect, url_for, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import requests
from selectolax.parser import HTMLParser
from urllib.parse import urlparse, urljoin
import re
import time
import json
import random
from flask_mail import Mail, Message
from email_validator import validate_email, EmailNotValidError
import phonenumbers
from phonenumbers import parse, is_valid_number, NumberParseException
from bs4 import BeautifulSoup
from collections import Counter
from nltk.util import ngrams
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# Flask app setup
app = Flask(__name__)
app.secret_key = 'supersecretkey'

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Dummy user store (now stores any username dynamically)
users = {}

class User(UserMixin):
    def __init__(self, username):
        self.id = username

@login_manager.user_loader
def load_user(user_id):
    return User(user_id) if user_id in users else None

# Helper functions for extracting information
def extract_info(page_tree, base_url):
    info = {
        'emails': set(),
        'phone_numbers': set(),
        'addresses': set(),
        'social_media': set(),
        'company_name': None,
    }
    # Patterns for extracting data
    email_pattern = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    phone_pattern = re.compile(r"\+?\d[\d\s\-\(\)]{7,}\d")
    address_patterns = [
        re.compile(pattern) for pattern in [
            r"\d{1,5}\s[\w\s.,-]{1,100},?\s[A-Z]{2}\s\d{5}",
            r"\d{1,5}\s[\w\s.,-]{1,100},?\s[\w\s.,-]{1,100},?\s[A-Z]{2}\s\d{5}",
            r"\d+\s[\w\s.,-]+\s[\w\s.,-]+,\s\w+\s[A-Z]{2}\s\d{5}",
        ]
    ]
    social_media_domains = {"facebook.com", "twitter.com", "linkedin.com", "instagram.com"}

    page_text = page_tree.body.text()

    # Extract emails, phone numbers, and addresses from text
    info['emails'].update(email_pattern.findall(page_text))
    info['phone_numbers'].update(phone_pattern.findall(page_text)[:2])
    for pattern in address_patterns:
        info['addresses'].update(pattern.findall(page_text))

    # Extract emails from 'mailto' links
    for node in page_tree.css('a[href^=mailto]'):
        email = node.attributes.get('href', '').split(':')[1].split('?')[0]
        info['emails'].add(email)

    # Extract social media links
    for node in page_tree.css('a[href]'):
        href = node.attributes.get('href', '')
        if href and any(domain in href for domain in social_media_domains):
            info['social_media'].add(urljoin(base_url, href))

    # Extract company name from structured data or meta tags
    for node in page_tree.css("script[type='application/ld+json']"):
        try:
            structured_data = json.loads(node.text())
            if "name" in structured_data:
                info['company_name'] = structured_data["name"]
                break
        except json.JSONDecodeError:
            continue

    if not info['company_name']:
        meta_name = page_tree.css_first('meta[property="og:site_name"]')
        if meta_name:
            info['company_name'] = meta_name.attributes.get('content', '')

    # Fallback to domain name if company name is still not found
    if not info['company_name']:
        domain = urlparse(base_url).netloc.split('.')
        if len(domain) > 1:
            business_name = domain[0].replace('www', '').replace('-', ' ').replace('_', ' ').title()
            if business_name.lower() not in ('top', '10', 'forbes', 'yelp', 'houzz', 'tripadvisor', 'angieslist', 'yellowpages', 'bbb'):
                info['company_name'] = business_name

    # Ensure all sets are converted to lists before returning
    return {k: list(v) if isinstance(v, set) else v for k, v in info.items()}

# Define the function to fetch page content
def fetch_page_content(url, headers):
    try:
        response = requests.get(url, headers=headers, timeout=10)  # Added timeout to avoid hanging indefinitely
        if response.status_code == 200:
            page_tree = HTMLParser(response.text)  # Use selectolax's HTMLParser
            return page_tree
        else:
            print(f"Failed to retrieve {url} with status code {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching content from {url}: {e}")
        return None

# Lead generator page route
@app.route('/leads-generator')
@login_required
def leads_generator():
    # Retrieve session data to display
    leads = session.get('leads', [])
    lead_count = session.get('lead_count', 0)
    email_count = session.get('email_count', 0)
    phone_count = session.get('phone_count', 0)
    address_count = session.get('address_count', 0)
    social_media_count = session.get('social_media_count', 0)
    company_name_count = session.get('company_name_count', 0)

    # Render the template and pass data
    return render_template(
        'leads_generator.html',
        title="Leads Generator",
        leads=leads,
        lead_count=lead_count,
        email_count=email_count,
        phone_count=phone_count,
        address_count=address_count,
        social_media_count=social_media_count,
        company_name_count=company_name_count
    )

# Main search route
@app.route('/search', methods=['POST'])
@login_required
def search():
    query = request.form.get('business_type')
    if not query:
        return redirect(url_for('leads_generator'))

    search_url = f"https://www.google.com/search?q={query}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    response = requests.get(search_url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')

    leads = []

    # Using ThreadPoolExecutor to fetch data in parallel
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}  # Using a dictionary to store futures and their corresponding link_node
        for g in soup.find_all('div', class_='g'):  # Assuming 'g' is the class for search result items
            title_node = g.find('h3')  # Get the first h3 element
            link_node = g.find('a')  # Get the first 'a' element
            domain = None
            
            if title_node:
                title_node = title_node.text.strip()  # Get the text content of the h3
            if link_node:
                link_node = link_node['href']  # Get the 'href' attribute
                domain = urlparse(link_node).netloc

            if title_node and link_node and domain:
                future = executor.submit(fetch_page_content, link_node, headers)
                futures[future] = link_node  # Store link_node in the dictionary with future as the key

    # Handle the results
    for future in as_completed(futures):
        page_content = future.result()
        link_node = futures[future]  # Retrieve the associated link_node for each future
        if page_content:
            base_url = urljoin(link_node, '/')
            info = extract_info(page_content, base_url)
            leads.append({
                'link': link_node, 
                'info': info
            })

    # Store leads data in session
    session['leads'] = leads  # Store the leads in session
    session['lead_count'] = len(leads)
    session['email_count'] = sum(len(lead['info'].get('emails', [])) for lead in leads)
    session['phone_count'] = sum(len(lead['info'].get('phone_numbers', [])) for lead in leads)
    session['address_count'] = sum(len(lead['info'].get('addresses', [])) for lead in leads)
    session['social_media_count'] = sum(len(lead['info'].get('social_media', [])) for lead in leads)
    session['company_name_count'] = sum(1 for lead in leads if lead['info'].get('company_name'))

    # Redirect to the leads generator page after updating session
    return redirect(url_for('leads_generator'))

# SEO

# Optimized function to extract keywords
def extract_keywords(text):
    words = re.findall(r'\b\w{3,}\b', text.lower())  # Only words with 3+ characters
    # Generate n-grams (bigrams, trigrams, etc.)
    ngrams_list = sum([[' '.join(gram) for gram in ngrams(words, n)] for n in range(2, 6)], [])
    # Combine words and n-grams
    phrases = words + ngrams_list
    stopwords = set(['the', 'in', 'and', 'or', 'is', 'it', 'to', 'from', 'by', 'with', 'for', 'on', 'at', 'as', 'this', 'that', 'these', 'those', 'i', 'we', 'they', 'you', 'll', 'pm', 'am'])
    return [phrase for phrase in phrases if not any(stopword in phrase for stopword in stopwords)]

def classify_keywords(keywords):
    short_tail = set()
    long_tail = set()
    for keyword in keywords:
        if len(keyword.split()) == 1:
            short_tail.add(keyword)
        else:
            long_tail.add(keyword)
    return short_tail, long_tail

def calculate_percentages(keywords, total):
    return {k: (v / total) * 100 for k, v in keywords.items()}

# Main route for SEO Boost
@app.route('/seo-boost', methods=['GET', 'POST'])
def seo_boost():
    if request.method == 'POST':
        query = request.form.get('business_name')
        if not query:
            return redirect(url_for('seo_boost'))

        search_terms = ["local business", "near me", "business directory"]
        search_queries = [f"{query} {term}" for term in search_terms]
        businesses = []
        all_keywords = []

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        # Create a session for reuse
        session = requests.Session()
        session.headers.update(headers)

        # Function to scrape page and extract keywords
        def process_page(href):
            try:
                page_response = session.get(href, timeout=30)
                page_response.raise_for_status()
                page_soup = BeautifulSoup(page_response.text, 'html.parser')

                # Extract text from paragraphs, meta descriptions, and headings
                page_text = " ".join([p.get_text() for p in page_soup.find_all(['p', 'meta', 'h1', 'h2', 'h3'])])
                return extract_keywords(page_text)
            except Exception as e:
                print(f"Error scraping {href}: {e}")
                return []

        # Parallelize requests using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            for search_query in search_queries:
                search_url = f"https://www.google.com/search?q={search_query}"

                try:
                    response = session.get(search_url)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, 'html.parser')

                    for g in soup.find_all('div', class_='tF2Cxc'):
                        link_node = g.find('a')
                        if link_node:
                            href = link_node['href']
                            domain = urlparse(href).netloc
                            if href and domain:
                                businesses.append({'url': href, 'domain': domain})
                                
                                # Asynchronously scrape the page for keywords
                                futures.append(executor.submit(process_page, href))
                
                except Exception as e:
                    print(f"Error during Google search scraping: {e}")
                    continue

            # Collect all keywords from page scraping
            for future in as_completed(futures):
                all_keywords.extend(future.result())

        # Count and classify keywords
        keyword_counts = Counter(all_keywords)
        keywords = {k: v for k, v in keyword_counts.items() if v > 1}  # Only include keywords that appear more than once
        total_keywords = sum(keywords.values())
        keyword_percentages = calculate_percentages(keywords, total_keywords)
        short_tail, long_tail = classify_keywords(keywords)

        return render_template('seo_boost.html', title="SEO Boost", short_tail=short_tail, long_tail=long_tail, keyword_percentages=keyword_percentages)
    else:
        return render_template('seo_boost.html', title="SEO Boost", short_tail=[], long_tail=[], keyword_percentages={})




# COMPETITOR ANALYSIS

# Function to scrape Google search results
def scrape_google_search(query):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    search_url = f"https://www.google.com/search?q={query}"
    response = requests.get(search_url, headers=headers)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    results = []
    for g in soup.find_all('div', class_='tF2Cxc'):
        title = g.find('h3').get_text() if g.find('h3') else 'No title'
        link = g.find('a')['href'] if g.find('a') else 'No link'
        domain = urlparse(link).netloc
        description = g.find('span', class_='aCOpRe').get_text() if g.find('span', class_='aCOpRe') else 'No description'
        
        # Filter out results that seem like lists or directories
        if "best" not in title.lower() and "directory" not in title.lower() and "list" not in title.lower() and "to know" not in title.lower() and "near me" not in title.lower():
            if "yelp.com" not in domain and "tripadvisor.com" not in domain and "houzz.com" not in domain:
                results.append({
                    'title': title,
                    'link': link,
                    'domain': domain,
                    'description': description
                })
    
    return results

# Function to get detailed information from a page
def get_real_info(url, session):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract title and meta description
        title = soup.find('title').get_text() if soup.find('title') else 'No title'
        description = soup.find('meta', {'name': 'description'})['content'] if soup.find('meta', {'name': 'description'}) else 'No description'

        # Extract specific service elements
        services = []
        for service_section in ['service-page', 'services', 'our-services', 'what-we-do']:
            service_page = soup.find('div', class_=service_section)
            if service_page:
                services = [item.get_text().strip() for item in service_page.find_all(['li', 'p']) if item.get_text().strip()]
                break
        
        # Fallback to extracting the first few paragraphs if no specific service section found
        if not services:
            paragraphs = soup.find_all('p')
            services = [paragraph.get_text().strip() for paragraph in paragraphs[:5] if paragraph.get_text().strip()]

        # Extracting business name
        business_name = soup.find('meta', property='og:site_name')
        if business_name:
            business_name = business_name['content']
        else:
            business_name = title.split(" - ")[0].strip() if " - " in title else title.split("|")[0].strip() if "|" in title else title.split(".")[0].strip()

        return {
            'business_name': business_name,
            'description': description,
            'services': services
        }
    except requests.exceptions.Timeout as e:
        return {
            'business_name': 'Error',
            'description': 'Connection timed out',
            'services': str(e)
        }
    except requests.exceptions.RequestException as e:
        return {
            'business_name': 'Error',
            'description': 'Failed to retrieve data',
            'services': str(e)
        }

# Function to rank businesses based on a simple rule
def rank_businesses(businesses):
    return sorted(businesses, key=lambda x: (x['description'] != 'No description', len(x['services'])), reverse=True)

# Flask route for competitor analysis
@app.route('/competitor_analysis', methods=['GET', 'POST'])
def competitor_analysis():
    if request.method == 'POST':
        business_type = request.form.get('business_type')
        location = request.form.get('location')
        if not business_type or not location:
            return redirect(url_for('competitor_analysis'))
        
        query = f"{business_type} {location}"
        search_results = scrape_google_search(query)

        # Create a session with retries
        session = requests.Session()
        retry = Retry(connect=5, backoff_factor=0.5)
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        # Using ThreadPoolExecutor to make multiple requests in parallel
        extracted_info = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(get_real_info, result['link'], session): result for result in search_results}

            for future in as_completed(futures):
                real_info = future.result()
                if real_info['business_name'] != 'Error' and real_info['services']:
                    extracted_info.append(real_info)

        ranked_businesses = rank_businesses(extracted_info)
        
        # Ensure at least 10 results
        while len(ranked_businesses) < 10:
            ranked_businesses.append({
                'business_name': 'No business found',
                'description': 'No description available',
                'services': ['No services available']
            })

        return render_template('competitor_analysis.html', title="Competitor Analysis", search_results=ranked_businesses)

    return render_template('competitor_analysis.html', title="Competitor Analysis", search_results=[])


#----------------------------------------------------------------

# FIND CONTACTS.
@app.route('/')
@login_required
def index():
    return render_template('dashboard.html', leads=[])

@app.route('/dashboard')
@login_required
def dashboard():
    # Retrieve data from session
    lead_count = session.get('lead_count', 0)
    email_count = session.get('email_count', 0)
    phone_count = session.get('phone_count', 0)
    address_count = session.get('address_count', 0)
    social_media_count = session.get('social_media_count', 0)
    company_name_count = session.get('company_name_count', 0)
    
    # Render the dashboard template, passing the counts
    return render_template('dashboard.html', lead_count=lead_count, email_count=email_count,
                           phone_count=phone_count, address_count=address_count,
                           social_media_count=social_media_count, company_name_count=company_name_count)




@app.route('/leads-generator', endpoint='unique_leads_generator')
@login_required
def leads_generator():
    # Your function logic here
    return render_template('leads_generator.html', title="Leads Generator")


@app.route('/find-contacts')
@login_required
def find_contacts():
    return render_template('find_contacts.html', title="Find Contacts")

@app.route('/social-media-lookup')
@login_required
def social_media_lookup():
    return render_template('social_media_lookup.html')



@app.route('/help_center')
@login_required
def help_center():
    return render_template('help_center.html')    



def parse_leads(leads_text):
    """
    Parses lead text into a list of lead information dictionaries.
    :param leads_text: A string containing lead details.
    :return: A list of dictionaries, each containing lead details.
    """
    # Split the leads text by "Visit Website", which is a unique identifier for the end of each lead
    lead_entries = leads_text.strip().split('Visit Website')
    leads = []

    for entry in lead_entries:
        if not entry.strip():
            continue
        
        lead_info = {}
        
        # Extract name (first line)
        name_match = re.match(r'^([^\n]+)', entry.strip())
        if name_match:
            lead_info['name'] = name_match.group(1).strip()
        
        # Extract emails
        emails_match = re.search(r'Email:\s*(.*)', entry)
        if emails_match:
            lead_info['emails'] = [email.strip() for email in emails_match.group(1).split(',')]

        # Extract phone numbers
        phones_match = re.search(r'Phone No:\s*(.*)', entry)
        if phones_match:
            lead_info['phone_numbers'] = [phone.strip() for phone in phones_match.group(1).split(',')]

        # Extract address
        address_match = re.search(r'Address:\s*(.*)', entry)
        if address_match:
            lead_info['address'] = address_match.group(1).strip()
        else:
            lead_info['address'] = 'None'

        # Extract social media links
        social_media_match = re.search(r'Social Media:\s*(.*)', entry)
        if social_media_match:
            lead_info['social_media'] = [link.strip() for link in social_media_match.group(1).split(',')]
        
        leads.append(lead_info)

    return leads



def score_lead(lead_info):
    """
    Generates a score, category, and conversion rate for a lead based on various criteria.
    :param lead_info: A dictionary containing lead details (name, emails, phone_numbers, address, social_media).
    :return: A dictionary with the score, category, and conversion rate for the lead.
    """
    score = 0

    # Scoring based on the number of emails
    if 'emails' in lead_info and lead_info['emails']:
        score += len(lead_info['emails']) * 10

    # Scoring based on the number of phone numbers
    if 'phone_numbers' in lead_info and lead_info['phone_numbers']:
        score += len(lead_info['phone_numbers']) * 5

    # Scoring based on presence of an address
    if 'address' in lead_info and lead_info['address'] and lead_info['address'] != 'None':
        score += 15

    # Scoring based on the number of social media links
    if 'social_media' in lead_info and lead_info['social_media']:
        score += len(lead_info['social_media']) * 10

    # Determine category based on score
    if score >= 50:
        category = 'High'
        conversion_rate = 90
    elif 30 <= score < 50:
        category = 'Medium'
        conversion_rate = 60
    else:
        category = 'Low'
        conversion_rate = 30

    return {
        'score': score,
        'category': category,
        'conversion_rate': conversion_rate
    }


@app.route('/ai-lead-scoring', methods=['GET', 'POST'])
def ai_lead_scoring():
    results = []
    if request.method == 'POST':
        # Get leads from form
        leads_text = request.form.get('leads', '')
        file = request.files.get('file')

        if file:
            leads_text = file.read().decode('utf-8')

        if leads_text.strip():
            # Simulate AI processing delay
            delay = random.uniform(2, 5)
            time.sleep(delay)

            # Parse the leads
            lead_infos = parse_leads(leads_text)

            # Process each lead
            for lead_info in lead_infos:
                score_info = score_lead(lead_info)
                score_info['score'] += random.randint(-5, 5)  # Add variability
                ai_feedback = f"AI has determined that the lead '{lead_info.get('name', 'No Name')}' has a {score_info['category']} potential."

                results.append({
                    'name': lead_info.get('name', 'No Name'),
                    'score': score_info['score'],
                    'category': score_info['category'],
                    'conversion_rate': score_info['conversion_rate'],
                    'email': ', '.join(lead_info.get('emails', [])),
                    'phone': ', '.join(lead_info.get('phone_numbers', [])),
                    'address': lead_info.get('address', 'None'),
                    'social_media': ', '.join(lead_info.get('social_media', [])),
                    'ai_feedback': ai_feedback
                })

    # Render the template with results
    return render_template('ai_lead_scoring.html', results=results)

def parse_leads(leads_text):
    """Mock function to parse leads from text."""
    # Example implementation (to be replaced with real parsing logic)
    return [
        {'name': 'John Doe', 'emails': ['john@example.com'], 'phone_numbers': ['123-456-7890'], 'address': '123 Main St', 'social_media': ['@johndoe']}
    ]

def score_lead(lead_info):
    """Mock function to score leads."""
    # Example scoring logic
    return {
        'score': random.randint(50, 100),
        'category': 'High' if random.random() > 0.5 else 'Medium',
        'conversion_rate': random.uniform(5, 15)
    }




@app.route('/search-company-contacts', methods=['POST'])
@login_required
def search_company_contacts():
    query = request.form.get('company_name')
    if not query:
        return redirect(url_for('find_contacts'))

    search_terms = ["ceo linkedin", "cto linkedin", "founder linkedin", "partner linkedin", "investor linkedin", "leadership linkedin"]
    search_queries = [f"{query} {term}" for term in search_terms]
    contacts = []
    unique_contacts = set()  # To track unique LinkedIn profiles

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    for search_query in search_queries:
        search_url = f"https://www.google.com/search?q={search_query}"
        try:
            response = requests.get(search_url, headers=headers)
            response.raise_for_status()  # Ensure we get a valid response
            soup = HTMLParser(response.text)

            for g in soup.css('div.g'):
                title_node = g.css_first('h3')
                link_node = g.css_first('a')

                if link_node:
                    href = link_node.attributes.get('href', '')
                    if not href:
                        continue

                    domain = urlparse(href).netloc

                    if title_node and domain and is_relevant_site(domain):
                        page_tree = fetch_page_content(href, headers)
                        if page_tree:
                            people = extract_people_info(page_tree)
                            for person in people:
                                if person['linkedin'] not in unique_contacts:
                                    contacts.append(person)
                                    unique_contacts.add(person['linkedin'])

        except Exception as e:
            print(f"An error occurred: {e}")
            continue

    # Filter out contacts without LinkedIn profile links (optional, if needed)
    contacts_with_profiles = [contact for contact in contacts if contact['linkedin']]

    return render_template('find_contacts.html', title="Company Contacts", contacts=contacts_with_profiles)


def extract_people_info(page_tree):
    people_info = []

    if not page_tree or not page_tree.body:
        return people_info

    # Patterns to identify people and their positions
    people_patterns = [
        re.compile(r"([A-Z][a-z]+ [A-Z][a-z]+)\s*(CEO|CTO|Founder|Partner|Investor|President|Vice President|Manager|Director|Head of [a-zA-Z]+|Lead [a-zA-Z]+)", re.IGNORECASE)
    ]

    # Extract people info from the text
    page_text = page_tree.body.text() if page_tree.body else ""

    for pattern in people_patterns:
        matches = pattern.findall(page_text)
        for match in matches:
            name, position = match
            linkedin_profile = find_linkedin_profile(page_tree, name)
            if linkedin_profile:
                people_info.append({
                    'name': name,
                    'position': position,
                    'linkedin': linkedin_profile
                })

    return people_info


def find_linkedin_profile(page_tree, name):
    if not page_tree:
        return None

    linkedin_profile = None
    for node in page_tree.css('a[href]'):
        href = node.attributes.get('href', '')
        if href and 'linkedin.com/in/' in href and name.lower().replace(' ', '-') in href.lower():
            linkedin_profile = href
            break
    return linkedin_profile

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # Simplified login to accept any username/password
        if username and password:
            # Add user to the session
            if username not in users:
                users[username] = generate_password_hash(password)
                
            user = User(username)
            login_user(user)

            # Set default session values to 0 for a fresh session
            session['lead_count'] = 0
            session['email_count'] = 0
            session['phone_count'] = 0
            session['address_count'] = 0
            session['social_media_count'] = 0
            session['company_name_count'] = 0
            
            # Redirect to the index or dashboard after login
            return redirect(url_for('index'))
        
        return 'Invalid credentials', 401
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()  # Logs out the user
    session.clear()  # Clears all session data
    return redirect(url_for('login'))  # Redirects to the login page (or any other page)




@app.route('/social-media-search', methods=['POST'])
@login_required
def perform_social_media_search():
    handle = request.form.get('social_media_handle')
    if not handle:
        return redirect(url_for('render_social_media_search'))

    search_url = f"https://www.google.com/search?q={handle} site:linkedin.com OR site:twitter.com OR site:instagram.com OR site:facebook.com OR site:tiktok.com OR site:pinterest.com OR site:youtube.com"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    response = requests.get(search_url, headers=headers)
    soup = HTMLParser(response.text)

    leads = []

    for g in soup.css('div.g'):
        link_node = g.css_first('a')
        snippet_node = g.css_first('span.aCOpRe')
        
        if link_node:
            profile_url = link_node.attributes.get('href', '')
            if any(platform in profile_url for platform in ['linkedin.com', 'twitter.com', 'instagram.com', 'facebook.com', 'tiktok.com', 'pinterest.com', 'youtube.com']):
                leads.append({
                    'social_media_handle': handle,
                    'profile_url': profile_url
                })

    return render_template('social_media_lookup.html', title="Social Media Search", leads=leads)

@app.route('/social-media-search')
@login_required
def render_social_media_search():
    return render_template('social_media_lookup.html', leads=None)


# Flask-Mail configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'  # Default SMTP server, can be overridden
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = None  # Set None to allow dynamic configuration
app.config['MAIL_PASSWORD'] = None

mail = Mail(app)


@app.route('/bulk_email_sender', methods=['GET', 'POST'])
@login_required
def bulk_email_sender():
    if request.method == 'POST':
        email_subject = request.form['email_subject']
        email_body = request.form['email_body']
        email_list = request.form['email_list'].split(',')
        smtp_server = request.form['smtp_server']
        smtp_port = request.form['smtp_port']
        smtp_username = request.form['smtp_username']
        smtp_password = request.form['smtp_password']

        # Update app config with user's SMTP details
        app.config['MAIL_SERVER'] = smtp_server
        app.config['MAIL_PORT'] = int(smtp_port)
        app.config['MAIL_USERNAME'] = smtp_username
        app.config['MAIL_PASSWORD'] = smtp_password

        try:
            with mail.connect() as conn:
                for email in email_list:
                    msg = Message(subject=email_subject,
                                  body=email_body,
                                  sender=smtp_username,
                                  recipients=[email.strip()])
                    conn.send(msg)
                    
            flash('Emails have been sent successfully!', 'success')
            return redirect(url_for('bulk_email_sender'))
        except Exception as e:
            flash(f'Failed to send emails. Error: {str(e)}', 'danger')
            return redirect(url_for('bulk_email_sender'))

    # Handle GET request: render the form
    return render_template('bulk_email_sender.html', title="Bulk Email Sender")


@app.route('/email-validation', methods=['GET', 'POST'])
@login_required
def email_validation():
    if request.method == 'POST':
        email_addresses = request.form['email_addresses'].splitlines()
        validation_results = []

        for email in email_addresses:
            email = email.strip()
            if email:  # Check if the email string is not empty
                try:
                    # Validate email without DNS checks
                    valid = validate_email(email, check_deliverability=False)
                    validation_results.append({
                        'email': email,
                        'status': 'Valid',
                        'status_class': 'text-success'
                    })
                except EmailNotValidError:
                    validation_results.append({
                        'email': email,
                        'status': 'Invalid',
                        'status_class': 'text-danger'
                    })

        return jsonify(validation_results)

    # Handle GET request: render the form
    return render_template('email_validation.html', title="Email Validation")


@app.route('/phone-validation', methods=['GET', 'POST'])
@login_required
def phone_validation():
    if request.method == 'POST':
        phone_numbers = request.form['phone_numbers'].splitlines()
        country_code = request.form.get('country_code', '').strip()
        validation_results = []

        for number in phone_numbers:
            number = number.strip()
            if number:
                try:
                    parsed_number = phonenumbers.parse(number, country_code if country_code else None)
                    if phonenumbers.is_valid_number(parsed_number):
                        validation_results.append({
                            'number': number,
                            'status': 'Valid',
                            'status_class': 'text-success'
                        })
                    else:
                        validation_results.append({
                            'number': number,
                            'status': 'Invalid',
                            'status_class': 'text-danger'
                        })
                except phonenumbers.NumberParseException:
                    validation_results.append({
                        'number': number,
                        'status': 'Invalid',
                        'status_class': 'text-danger'
                    })

        return jsonify(validation_results)

    return render_template('phone_validation.html', title="Phone Number Validation")



if __name__ == '__main__':
    app.run(threaded=True)
