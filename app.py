import sqlite3
import requests
import re
import markdown
from markupsafe import Markup
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DB_NAME = "papers.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS folders (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)')
        cursor.execute('INSERT OR IGNORE INTO folders (id, name) VALUES (1, "📥 Inbox")')

        # 新增 authors, published_date, bibtex 欄位
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER DEFAULT 1,
                url TEXT NOT NULL,
                title TEXT,
                authors TEXT,
                published_date TEXT,
                bibtex TEXT,
                notes TEXT,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(folder_id) REFERENCES folders(id)
            )
        ''')
        cursor.execute('CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_tags (
                paper_id INTEGER, tag_id INTEGER,
                PRIMARY KEY (paper_id, tag_id),
                FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        ''')
        conn.commit()

def generate_bibtex(url, title, authors, year, arxiv_id=None):
    """產生 Google Scholar 風格的 BibTeX"""
    # 取第一位作者的姓氏
    first_author = authors.split(',')[0].split(' ')[-1] if authors else "Unknown"
    # 生成 Key (e.g., smith2023attention)
    first_word_title = title.split(' ')[0].lower() if title else "paper"
    key = f"{first_author.lower()}{year}{first_word_title}"
    
    # 移除 BibTeX 不喜歡的特殊字元
    key = re.sub(r'[^a-zA-Z0-9]', '', key)

    bibtex = f"@article{{{key},\n"
    bibtex += f"  title={{{title}}},\n"
    bibtex += f"  author={{{authors.replace(',', ' and')}}},\n"
    
    if arxiv_id:
        bibtex += f"  journal={{arXiv preprint arXiv:{arxiv_id}}},\n"
    else:
        bibtex += f"  journal={{ArXiv / HuggingFace}},\n"
        
    bibtex += f"  year={{{year}}},\n"
    bibtex += f"  url={{{url}}}\n"
    bibtex += "}"
    return bibtex

def fetch_metadata(url):
    """抓取標題、作者、日期並生成 BibTeX"""
    data = {
        "title": "Unknown Title",
        "authors": "",
        "published_date": "",
        "bibtex": ""
    }
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # === Arxiv 處理 ===
            if 'arxiv.org' in url:
                # Title
                t_tag = soup.find('h1', class_='title')
                if t_tag:
                    data['title'] = t_tag.text.replace('Title:', '').strip()
                
                # Authors
                auth_tag = soup.find('div', class_='authors')
                if auth_tag:
                    authors = [a.text.strip() for a in auth_tag.find_all('a')]
                    data['authors'] = ", ".join(authors)

                # Date (Parsing "Submitted on 6 Dec 2023")
                date_tag = soup.find('div', class_='dateline')
                year = datetime.now().year
                if date_tag:
                    date_text = date_tag.text.strip()
                    # 使用 Regex 抓取日期
                    match = re.search(r'Submitted on\s+(\d{1,2}\s+\w+\s+\d{4})', date_text)
                    if match:
                        date_str = match.group(1)
                        try:
                            dt = datetime.strptime(date_str, "%d %b %Y")
                            data['published_date'] = dt.strftime("%Y-%m-%d")
                            year = dt.year
                        except:
                            pass
                
                # Arxiv ID for BibTeX
                arxiv_id = url.split('/')[-1].replace('.pdf', '')
                data['bibtex'] = generate_bibtex(url, data['title'], data['authors'], year, arxiv_id)

            # === Hugging Face Daily Papers 處理 ===
            elif 'huggingface.co' in url:
                t_tag = soup.find('h1')
                if t_tag: data['title'] = t_tag.text.strip()
                
                # HF 的結構比較多變，嘗試抓取作者
                # 這裡做個簡單的假設，HF 論文頁面通常會有作者連結
                author_tags = soup.select('a[href*="/papers?author="]') 
                if author_tags:
                    authors = [a.text.strip() for a in author_tags]
                    data['authors'] = ", ".join(authors)
                
                # 簡單設定日期為今日 (HF 頁面日期較難通抓)
                data['published_date'] = datetime.now().strftime("%Y-%m-%d")
                year = datetime.now().year
                data['bibtex'] = generate_bibtex(url, data['title'], data['authors'], year)

            # === 通用處理 ===
            else:
                if soup.title: data['title'] = soup.title.string.strip()
                data['published_date'] = datetime.now().strftime("%Y-%m-%d")
                data['bibtex'] = generate_bibtex(url, data['title'], "Unknown", datetime.now().year)

    except Exception as e:
        print(f"Error fetching metadata: {e}")
    
    return data

def process_tags(conn, paper_id, tags_str):
    if not tags_str: return
    conn.execute("DELETE FROM paper_tags WHERE paper_id = ?", (paper_id,))
    tag_list = [t.strip() for t in tags_str.split(',') if t.strip()]
    for tag_name in tag_list:
        cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
        row = cursor.fetchone()
        tag_id = row['id'] if row else conn.execute("INSERT INTO tags (name) VALUES (?)", (tag_name,)).lastrowid
        conn.execute("INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)", (paper_id, tag_id))

@app.template_filter('markdown')
def render_markdown(text):
    if not text:
        return ""
    # 啟用 fenced_code (程式碼區塊) 和 tables (表格) 擴充
    return Markup(markdown.markdown(text, extensions=['fenced_code', 'tables', 'nl2br']))

@app.route('/')
def index():
    folder_id = request.args.get('folder_id')
    tag_id = request.args.get('tag_id')
    status = request.args.get('status')
    
    conn = get_db()
    query = """
        SELECT p.*, f.name as folder_name, GROUP_CONCAT(t.name) as tag_names 
        FROM papers p
        LEFT JOIN folders f ON p.folder_id = f.id
        LEFT JOIN paper_tags pt ON p.id = pt.paper_id
        LEFT JOIN tags t ON pt.tag_id = t.id
    """
    conditions, params = [], []
    if folder_id:
        conditions.append("p.folder_id = ?")
        params.append(folder_id)
    if tag_id:
        conditions.append("p.id IN (SELECT paper_id FROM paper_tags WHERE tag_id = ?)")
        params.append(tag_id)
    if status == 'unread': conditions.append("p.is_read = 0")
    elif status == 'read': conditions.append("p.is_read = 1")

    if conditions: query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY p.id ORDER BY p.created_at DESC"
    
    papers = conn.execute(query, params).fetchall()
    folders = conn.execute("SELECT * FROM folders").fetchall()
    tags = conn.execute("SELECT t.id, t.name, COUNT(pt.paper_id) as count FROM tags t LEFT JOIN paper_tags pt ON t.id = pt.tag_id GROUP BY t.id").fetchall()
    conn.close()
    
    current_filter = "所有論文"
    if status == 'unread': current_filter = "未讀清單"
    if status == 'read': current_filter = "已讀封存"
    if folder_id: 
        for f in folders: 
            if str(f['id']) == str(folder_id): current_filter = f"📁 {f['name']}"
    if tag_id:
        for t in tags:
            if str(t['id']) == str(tag_id): current_filter = f"🏷️ {t['name']}"

    return render_template('index.html', papers=papers, folders=folders, tags=tags, 
                           current_filter=current_filter, active_folder=folder_id, active_tag=tag_id)

@app.route('/paper/<int:paper_id>')
def paper_detail(paper_id):
    conn = get_db()
    
    # 取得論文詳細資料
    paper = conn.execute("""
        SELECT p.*, f.name as folder_name, GROUP_CONCAT(t.name) as tag_names 
        FROM papers p
        LEFT JOIN folders f ON p.folder_id = f.id
        LEFT JOIN paper_tags pt ON p.id = pt.paper_id
        LEFT JOIN tags t ON pt.tag_id = t.id
        WHERE p.id = ?
        GROUP BY p.id
    """, (paper_id,)).fetchone()
    
    if not paper:
        return redirect(url_for('index'))

    folders = conn.execute("SELECT * FROM folders").fetchall()
    conn.close()
    
    return render_template('paper.html', paper=paper, folders=folders)

@app.route('/add_paper', methods=['POST'])
def add_paper():
    url = request.form.get('url')
    folder_id = request.form.get('folder_id', 1)
    tags_str = request.form.get('tags')
    
    if url:
        meta = fetch_metadata(url)
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO papers (url, title, authors, published_date, bibtex, folder_id, notes) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                (url, meta['title'], meta['authors'], meta['published_date'], meta['bibtex'], folder_id, "")
            )
            process_tags(conn, cursor.lastrowid, tags_str)
            conn.commit()
    return redirect(url_for('index'))

@app.route('/add_folder', methods=['POST'])
def add_folder():
    name = request.form.get('name')
    if name:
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO folders (name) VALUES (?)", (name,))
            conn.commit()
    return redirect(url_for('index'))

@app.route('/update/<int:paper_id>', methods=['POST'])
def update_paper(paper_id):
    notes = request.form.get('notes')
    is_read = 1 if request.form.get('is_read') else 0
    folder_id = request.form.get('folder_id')
    tags_str = request.form.get('tags_input')
    bibtex = request.form.get('bibtex')

    with get_db() as conn:
        conn.execute("UPDATE papers SET notes = ?, is_read = ?, folder_id = ?, bibtex = ? WHERE id = ?", 
                     (notes, is_read, folder_id, bibtex, paper_id))
        if tags_str is not None:
             # 因為詳情頁可能會直接傳送 tags input，所以這裡要處理
             # 注意：詳情頁的 update 邏輯要共用 process_tags
             from app import process_tags # 示意
             process_tags(conn, paper_id, tags_str)
        conn.commit()
    
    # 智慧導向：如果是從詳情頁來的，就回到詳情頁；否則回首頁
    referrer = request.referrer
    if referrer and 'paper' in referrer:
        return redirect(url_for('paper_detail', paper_id=paper_id))
    return redirect(url_for('index'))

@app.route('/delete/<int:paper_id>', methods=['POST'])
def delete_paper(paper_id):
    with get_db() as conn:
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        conn.commit()
    return redirect(url_for('index'))

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)