#!/usr/bin/env python3
"""
Yaazhi CI Script — Run tests, check coverage, and generate badge.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

def main():
    print("🚀 Running Yaazhi Test Suite with Coverage...")
    
    # Ensure docs directory exists
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    
    # 1. Run pytest with coverage
    cmd = [
        "pytest",
        "--cov=.",
        "--cov-report=term-missing",
        "--cov-fail-under=80"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout + result.stderr
    print(output)
    
    # 2. Parse coverage output
    coverage_pct = 0.0
    match = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if match:
        coverage_pct = float(match.group(1))
    
    # 3. Write coverage badge to docs/coverage.svg
    color = "success" if coverage_pct >= 80 else "critical"
    badge_url = f"https://img.shields.io/badge/coverage-{int(coverage_pct)}%25-{color}"
    
    try:
        import urllib.request
        req = urllib.request.Request(badge_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            svg_data = response.read()
            (docs_dir / "coverage.svg").write_bytes(svg_data)
        print(f"✅ Generated coverage badge at docs/coverage.svg ({coverage_pct}%)")
    except Exception as e:
        print(f"⚠️ Could not download badge: {e}")
    
    # 4. If coverage < 80%: print all uncovered files and exit 1
    if result.returncode != 0 or coverage_pct < 80:
        print("\n❌ CRITICAL: Test coverage fell below 80% threshold!")
        
        # Extract and print files with missing coverage
        print("\nFiles needing tests:")
        lines = output.split('\n')
        in_table = False
        for line in lines:
            if "--------" in line:
                in_table = not in_table
                continue
            if in_table and "TOTAL" not in line and "%" in line:
                parts = line.split()
                if len(parts) >= 4 and int(parts[3].replace("%", "")) < 100:
                    missing_lines = " ".join(parts[4:]) if len(parts) > 4 else ""
                    print(f"  - {parts[0]}: {parts[3]} (Missing: {missing_lines})")
                    
        sys.exit(1)
    
    print("\n✅ All tests passed! Coverage is above 80%.")
    sys.exit(0)

if __name__ == "__main__":
    main()
