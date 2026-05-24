# Equity Analyzer

Equity Analyzer is an intuitive web-based dashboard designed to track, visualize, and analyze how stock prices react to corporate earnings releases. By cross-referencing earnings surprises with immediate market price actions (like opening price gaps), the tool helps investors quickly spot patterns, identify post-earnings momentum, and uncover potential market inefficiencies.

### Key Features
* **Interactive Earnings Matrix:** Visualizes the correlation between earnings and forward guidance surprises and next-day price movements.
* **Real-Time Tracking:** Automatically logs and displays data aligned to the active financial quarter (earnings season).

## How to Run the Application
### Step 1: Download the Project
1. Download this project repository as a `.zip` file to your computer.
2. Extract (unzip) the files into a folder of your choice.

### Step 2: Set Up a Virtual Environment
* **Create the environment (All Systems):**
  ```bash
  python -m venv .venv
  ```

* Activate it (Choose based on your system):
  * Windows (Command Prompt): `.venv\Scripts\activate`
  * Windows (PowerShell): `.\.venv\Scripts\Activate.ps1`
  * macOS / Linux: `source .venv/bin/activate`

*(Upon successful activation, you should see `(.venv)` appear at the very beginning of your terminal line.)*

### Step 3: Install the Required Tools

Run the following command to automatically install all necessary libraries and software dependencies:

```bash
pip install -r requirements.txt
```

### Step 4: Launch the Web App

Start the local server by typing:

```bash
flask run
```
---

## Accessing the Dashboard

Once the app is running, your terminal will display a local web address. Open your favorite web browser (Chrome, Edge, Safari, etc.) and go to:

**[http://127.0.0.1:5000](http://127.0.0.1:5000)**

*To stop the application at any time, go back to your terminal window and press `Ctrl + C`.*
