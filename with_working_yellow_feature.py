import os
from flask import Flask, render_template, request, redirect, url_for, g, session
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
import threading

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Add secret key for session management

# Configuration
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), 'credentials.json')

# Thread-local storage for sheets service
thread_local = threading.local()

def get_sheets_service():
    """Get or create sheets service for current thread"""
    if not hasattr(thread_local, 'sheets_service'):
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        thread_local.sheets_service = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    return thread_local.sheets_service

# Batch size for reading multiple rows at once
BATCH_SIZE = 10

# Clear cache when sheet ID changes
def clear_cache():
    fetch_batch_data.cache_clear()

@lru_cache(maxsize=1000)
def fetch_batch_data(spreadsheet_id, start_row, batch_size=BATCH_SIZE):
    """Fetch multiple rows at once and cache the results"""
    if not spreadsheet_id:
        return None
    
    service = get_sheets_service()
    range_ = f"E{start_row}:I{start_row + batch_size - 1}"
    
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, 
            range=range_
        ).execute()
        return result.get('values', [])
    except Exception as e:
        app.logger.error(f"Error fetching batch data: {e}")
        return None

def get_row_data(spreadsheet_id, row_number):
    """Get data for a specific row from the batch cache"""
    batch_start = (row_number // BATCH_SIZE) * BATCH_SIZE + 1
    batch_data = fetch_batch_data(spreadsheet_id, batch_start)
    
    if not batch_data:
        return None, None
    
    row_index = (row_number - batch_start)
    if row_index >= len(batch_data):
        return None, None
    
    row = batch_data[row_index]
    if len(row) < 5:  # Ensure we have all needed columns
        return None, None
    
    product_data = {'product_url': row[0], 'product_name': row[1]}
    row_data = row[2:5] + [''] * (3 - len(row[2:5]))  # Pad with empty strings if needed
    
    return product_data, row_data

def update_sheet_data(spreadsheet_id, row_number, updates):
    """Batch update for both cell values and formatting"""
    if not spreadsheet_id:
        return

    service = get_sheets_service()
    
    # Prepare requests for batch update
    requests = []
    
    # Update cell values if provided
    if 'values' in updates:
        range_ = f"G{row_number}:I{row_number}"
        body = {'values': [updates['values']]}
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_,
            valueInputOption="RAW",
            body=body
        ).execute()

    # Update cell colors if provided
    if 'color' in updates:
        color = updates['color']
        for col_index in range(6, 8):  # Columns G and H
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": 0,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })

    # Add yellow highlight if captions were edited
    if updates.get('edited'):
        yellow_color = {'red': 1, 'green': 1, 'blue': 0, 'alpha': 1}
        for col_index in range(6, 8):  # Columns G and H
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": 0,
                        "startRowIndex": row_number - 1,
                        "endRowIndex": row_number,
                        "startColumnIndex": col_index,
                        "endColumnIndex": col_index + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": yellow_color
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })

    # Add comment if provided
    if 'comment' in updates:
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": 0,
                    "startRowIndex": row_number - 1,
                    "endRowIndex": row_number,
                    "startColumnIndex": 7,
                    "endColumnIndex": 8
                },
                "rows": [{"values": [{"note": updates['comment']}]}],
                "fields": "note"
            }
        })

    # Execute batch update if there are any requests
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        new_sheet_id = request.form.get('sheet_id')
        if new_sheet_id != session.get('sheet_id'):
            session['sheet_id'] = new_sheet_id
            clear_cache()  # Clear cache when sheet ID changes
        return redirect(url_for('sheet_view', row_number=2))
    return render_template('index.html')

@app.route('/sheet', methods=['GET', 'POST'])
def sheet_view():
    spreadsheet_id = session.get('sheet_id')
    if not spreadsheet_id:
        return redirect(url_for('index'))
        
    row_number = int(request.args.get('row_number', 2))
    
    product_data, row_data = get_row_data(spreadsheet_id, row_number)
    
    if not product_data or not row_data:
        return redirect(url_for('no_data'))
    
    short_desc, long_desc, image_link = row_data
    short_desc_word_count = len(short_desc.split()) if short_desc else 0
    long_desc_word_count = len(long_desc.split()) if long_desc else 0

    if request.method == 'POST':
        action = request.form.get('action')
        updates = {}

        # Prepare value updates
        updated_short_desc = request.form.get('short_desc', short_desc)
        updated_long_desc = request.form.get('long_desc', long_desc)
        captions_edited = updated_short_desc != short_desc or updated_long_desc != long_desc
        if captions_edited:
            updates['values'] = [updated_short_desc, updated_long_desc, image_link]

        # Set color based on action
        if action == 'approve':
            if captions_edited:  # Turn cells yellow if captions were edited
                updates['color'] = {'red': 1, 'green': 1, 'blue': 0, 'alpha': 1}
            else:  # Default green for approve
                updates['color'] = {'red': 0, 'green': 1, 'blue': 0, 'alpha': 1}
        elif action == 'reject':  # Always turn cells red for reject
            updates['color'] = {'red': 1, 'green': 0, 'blue': 0, 'alpha': 1}

        # Prepare comment update
        comment = request.form.get('comment')
        if comment:
            updates['comment'] = comment

        # Perform batch update
        if updates:
            update_sheet_data(spreadsheet_id, row_number, updates)

        return redirect(url_for('sheet_view', row_number=row_number + 1))

    return render_template(
        'sheet_view.html',
        row_number=row_number,
        short_desc=short_desc,
        long_desc=long_desc,
        image_link=image_link,
        product_name=product_data['product_name'],
        product_url=product_data['product_url'],
        short_desc_word_count=short_desc_word_count,
        long_desc_word_count=long_desc_word_count
    )


@app.route('/no_data', methods=['GET', 'POST'])
def no_data():
    if request.method == 'POST':
        row_number = int(request.form.get('row_number', 2))
        return redirect(url_for('sheet_view', row_number=row_number))
    return render_template('no_data.html')

if __name__ == '__main__':
    app.run(debug=True, threaded=True)
