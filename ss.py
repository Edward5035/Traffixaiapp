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
        results.append({
            'title': title,
            'link': link,
            'domain': domain,
            'description': description
        })
    return results

# Function to get detailed information from a page
def get_real_info(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    session = requests.Session()
    retry = Retry(connect=5, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    try:
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract title and meta description
        title = soup.find('title').get_text() if soup.find('title') else 'No title'
        description = soup.find('meta', {'name': 'description'})['content'] if soup.find('meta', {'name': 'description'}) else 'No description'

        # Extract snippet, searching first for specific service elements, then falling back to the first paragraph
        service_page = soup.find('div', class_='service-page') or soup.find('div', class_='services')
        if service_page:
            services = service_page.get_text()
        else:
            snippet = soup.find('p').get_text() if soup.find('p') else 'No snippet'
            services = snippet

        return {
            'title': title,
            'description': description,
            'services': services
        }
    except requests.exceptions.Timeout as e:
        return {
            'title': 'Error',
            'description': 'Connection timed out',
            'services': str(e)
        }
    except requests.exceptions.RequestException as e:
        return {
            'title': 'Error',
            'description': 'Failed to retrieve data',
            'services': str(e)
        }

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
        extracted_info = []
        for result in search_results:
            real_info = get_real_info(result['link'])
            extracted_info.append({
                'title': result['title'],
                'link': result['link'],
                'domain': result['domain'],
                'description': real_info['description'],
                'services': real_info['services']
            })
        
        return render_template('competitor_analysis.html', title="Competitor Analysis", search_results=extracted_info)

    return render_template('competitor_analysis.html', title="Competitor Analysis", search_results=[])
