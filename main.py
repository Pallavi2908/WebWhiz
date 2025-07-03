import os, chromadb
from openai import OpenAI
from chromadb.config import Settings
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from markdownify import markdownify
import urllib.parse, time
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)


# LLM client
load_dotenv(override=True)
anthropic_client = OpenAI(
  base_url="https://openrouter.ai/api/v1", #OpenRouter provides an OpenAI-compatible completion API 
  api_key=os.getenv("API_KEY"),
)

#embeddings client : OpenAI sentence-transformers/all-MiniLM-L6-v2
openai_client= OpenAI(api_key=os.getenv("OPENAI_KEY"))

def query_classifier(query:str)->str:
    res=anthropic_client.chat.completions.create(
        model="anthropic/claude-3.5-haiku",
        messages=[
            {
                "role" : "system",
                "content" : open("context.md").read()
            },
            {
                "role":"user",
                "content":query
            }
        ],
        temperature=0.2, ##  0.2 is best to avoid creative outputs as we only generate binary outputs : any range from 0-0.2 is perfect for this
        max_tokens=2, #context md will only say VALID or INVALID
        stop=['\n']
    )
    print("Received query")
    result=res.choices[0].message.content.strip().upper()
    return result

#using embedding model on HF
embed_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
def get_embedding(query: str) -> list[float]:
    # Clean and truncate the query
    clean_query = ' '.join(query.strip().split())[:512]  # Model's max length
    return embed_model.encode(clean_query).tolist()

#vectorDB fn -> calling in chromaDB
chroma_client = chromadb.PersistentClient(
    # we are going to save and load db locally!
    path="./vector_db", #path to save db files
    settings=Settings(anonymized_telemetry=False)
)

collection = chroma_client.get_or_create_collection(
    name="search_results",
    metadata={"hnsw:space": "cosine"}  # Using cosine similarity
)

def normalize_query(query: str) -> str:
    """Normalize queries for better similarity matching"""
    query = query.lower().strip()
    synonyms = {
        "iconic": "famous",
        "best": "top",
        "bookshops": "bookstores",
        "bookstores": "bookshops"
    }
    for word, replacement in synonyms.items():
        query = query.replace(word, replacement)
    return ' '.join(query.split())  # Remove extra whitespace

def find_similar_results(query: str, threshold: float = 0.72) -> dict:
    normalized_query = normalize_query(query)
    embedding = get_embedding(normalized_query)
    
    results = collection.query(
        query_embeddings=[embedding],
        n_results=5,
        include=["metadatas", "distances", "documents"]
    )
    
    print(f"\nSimilarity check for: '{query}' (normalized: '{normalized_query}')")
    print("Top matches:")
    for i, (meta, dist) in enumerate(zip(results['metadatas'][0], results['distances'][0])):
        print(f"{i+1}. {meta['original_query']} (similarity: {1-dist:.2f})")
    

    if results["distances"] and len(results["distances"][0]) > 0:
        best_idx = 0
        best_similarity = 1 - results["distances"][0][best_idx]
        
        if best_similarity >= threshold:
            best_match = {
                "query": results["metadatas"][0][best_idx]["original_query"],
                "summary": results["metadatas"][0][best_idx]["summary"],
                "url": results["metadatas"][0][best_idx]["url"],
                "content": results["documents"][0][best_idx] if results["documents"] else None,
                "similarity": best_similarity
            }
            print(f"Found match with similarity {best_similarity:.2f}")
            return best_match
        else:
            print(f"No matches above threshold (best was {best_similarity:.2f})")
    
    return None

def search_and_scrape(query: str, num_results: int = 6) -> list[dict]:
    search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
    results = []
    
    playwright_instance = sync_playwright()
    playwright = playwright_instance.start()
    browser = None
    context = None
    
    try:
        browser = playwright.chromium.launch(
            headless=True,
            timeout=60000,  # 60 second launch timeout
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox'
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 800},
            locale='en-US',
            java_script_enabled=True
        )
        
        page = context.new_page()
        
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
                page.wait_for_timeout(1000)  
        except:
            pass  
        
        try:
            page.wait_for_selector('div#rso', timeout=10000)
        except:
            print("Search results not found")
            return results
        
        links = page.query_selector_all('a[jsname="UWckNb"], a[jsname="YKoRaf"]')
        urls = []
        
        for link in links[:num_results]:
            href = link.get_attribute('href')
            if href and href.startswith('http') and 'google.com' not in href:
                urls.append(href)
        
        print(f"Found {len(urls)} valid URLs to scrape")
        
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



#storing the VALID search results

def store_result(query: str, scraped_results: list[dict]):
    embedding = get_embedding(query)
    
    for item in scraped_results:
        url = item["url"]
        content = item["content"]
        
        # Summarize each page content (50 words max)
        summary = summarize_content(content)  
        
        collection.add(
            embeddings=[embedding],  
            documents=[content],     
            metadatas=[{
                "original_query": query,
                "summary": summary,
                "url": url
            }],
            ids=[f"{hash(url)}"]  # Unique ID per URL
        )


#LLM to summarize content (50 words) per page
def summarize_content(text: str) -> str:
    
    clean_text = ' '.join(text.strip().split())[:2000]  # Remove extra whitespace and limit length
    
    prompt = f"""Please provide a concise, accurate summary of the following text following these guidelines:
    
1. Length: Approximately 50 words (strictly between 45-55 words)
2. Style: Professional, factual, and neutral tone
3. Content: Focus on key points, main ideas, and essential information
4. Omit: Examples, anecdotes, and repetitive information
5. Structure: Single coherent paragraph with complete sentences

Text to summarize:
{clean_text}

Summary:"""
    
    try:
        res = anthropic_client.chat.completions.create(
            model="anthropic/claude-3.5-haiku",
            messages=[
                {
                    "role": "system", 
                    "content": "You are an expert summarizer who creates precise, factual summaries while preserving all key information."
                },
                {
                    "role": "user", 
                    "content": prompt
                }
            ],
            temperature=0.3,  # Slightly higher for better phrasing while maintaining accuracy
            max_tokens=150,   
            stop=["\n\n"]     
        )
        
        summary = res.choices[0].message.content.strip()
        
        summary = ' '.join(summary.split()) 
        if not summary.endswith('.'):
            summary += '.'  
            
        return summary
        
    except Exception as e:
        print(f"Summarization failed: {str(e)}")
        return "Summary not available."
    

def store_scraped_results(query: str, scraped_results: list[dict]):
    embedding = get_embedding(query)
    
    for i, item in enumerate(scraped_results):
        url = item.get("url")
        content = item.get("content", "")

        summary = summarize_content(content)

        unique_id = f"{hash(url)}"

        try:
            collection.add(
                embeddings=[embedding],
                documents=[content],
                metadatas=[{
                    "original_query": query,
                    "summary": summary,
                    "url": url
                }],
                ids=[unique_id]
            )
            print(f"Stored result for: {url}")
        except Exception as e:
            print(f"Storage failed for {url}: {str(e)}")


@app.route('/')
def home():
    return render_template('index.html')  

@app.route('/search', methods=['POST'])
def search():
    data = request.json
    query = data.get('query')
    force = data.get('force', False)
    
    # Step 1: Classify
    result = query_classifier(query)
    if result != "VALID":
        return jsonify({"error": "This is not a valid query."})
    
    # Step 2: Check for similar result (unless forced)
    if not force:
        past_result = find_similar_results(query)
        if past_result:
            return jsonify({
                "similar": {
                    "query": past_result["query"],
                    "summary": past_result["summary"],
                    "url": past_result["url"]
                }
            })
    
    results = search_and_scrape(query)
    if results:
        store_scraped_results(query, results)
        return jsonify({
            "results": [{
                "query": query,
                "url": r["url"],
                "summary": summarize_content(r["content"])
            } for r in results]
        })
    return jsonify({"error": "No results found."})

if __name__ == '__main__':
    app.run(debug=True)