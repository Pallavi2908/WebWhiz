def search_and_scrape(query: str, num_results: int = 5) -> list[dict]:
    search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    results = []
    
    # Initialize Playwright
    playwright_instance = sync_playwright()
    playwright = playwright_instance.start()
    browser = None
    context = None
    
    try:
        # Launch browser with increased timeout
        browser = playwright.chromium.launch(
            headless=True,
            timeout=60000,  # 60 second launch timeout
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox'
            ]
        )
        
        # Create context with proper settings
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
            java_script_enabled=True
        )
        
        # Create main page
        page = context.new_page()
        
        # Navigation with multiple safeguards
        try:
            print(f"Navigating to: {search_url}")
            response = page.goto(
                search_url,
                timeout=30000,
                wait_until="domcontentloaded"
            )
            
            if not response or not response.ok:
                print(f"Navigation failed with status: {response.status if response else 'No response'}")
                return results
                
        except Exception as e:
            print(f"Navigation error: {str(e)}")
            return results
        
        # Cookie consent handling
        try:
            accept_button = page.wait_for_selector(
                ':text("Accept all"), :text("Accept"), :text("I agree")',
                timeout=5000,
                state="visible"
            )
            if accept_button:
                accept_button.click()
                page.wait_for_timeout(1000)  # Wait for consent to process
        except:
            pass  # No cookie dialog found
        
        # Wait for search results
        try:
            page.wait_for_selector('div#rso', timeout=10000)
        except:
            print("Search results not found")
            return results
        
        # Get search result links
        links = page.query_selector_all('a[jsname="UWckNb"], a[jsname="YKoRaf"]')
        urls = []
        
        for link in links[:num_results]:
            href = link.get_attribute('href')
            if href and href.startswith('http') and 'google.com' not in href:
                urls.append(href)
        
        print(f"Found {len(urls)} valid URLs to scrape")
        
        # Scrape each URL
        for url in urls:
            try:
                print(f"Scraping: {url}")
                tab = context.new_page()
                
                try:
                    tab_response = tab.goto(
                        url,
                        timeout=20000,
                        wait_until="domcontentloaded"
                    )
                    
                    if not tab_response or not tab_response.ok:
                        print(f"Failed to load {url} - Status: {tab_response.status if tab_response else 'No response'}")
                        continue
                        
                    # Get and process content
                    html = tab.content()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    # Remove unwanted elements
                    for element in soup(['script', 'style', 'nav', 'footer', 'iframe', 'noscript']):
                        element.decompose()
                    
                    # Find main content
                    main_content = soup.find('main') or soup.find('article') or soup.body
                    content = main_content.get_text(' ', strip=True)[:20000] if main_content else ""
                    
                    results.append({
                        'url': url,
                        'content': content,
                        'scrape_time': time.time()
                    })
                    
                except Exception as e:
                    print(f"Error scraping {url}: {str(e)}")
                finally:
                    tab.close()
                    
            except Exception as e:
                print(f"Error creating tab for {url}: {str(e)}")
        
        return results
        
    except Exception as e:
        print(f"Main scraping error: {str(e)}")
        return results
    finally:
        # Proper cleanup in reverse order
        try:
            if context:
                context.close()
        except:
            pass
        
        try:
            if browser:
                browser.close()
        except:
            pass
        
        try:
            playwright.stop()
        except:
            pass
