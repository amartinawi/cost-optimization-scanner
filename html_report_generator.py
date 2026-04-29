#!/usr/bin/env python3
"""
HTML Report Generator for AWS Cost Optimization Scanner v2.5.9

This module generates professional, interactive HTML reports from cost optimization
scan results. The reports feature a multi-tab interface with smart grouping,
zero duplication, and consistent styling across all 31 AWS services.

Key Features:
- Interactive multi-tab interface for easy navigation
- Smart grouping by optimization category for better organization
- Zero duplication across all data sources
- Professional styling with consistent formatting
- Empty tab hiding for clean presentation
- Profile-based filenames for multi-account management
- Responsive design for desktop and mobile viewing
- Enhanced error handling with proper logging
- Cross-region support with accurate reporting

The generator processes scan results from 31 AWS services and creates:
- Service-specific tabs with recommendations
- Statistics cards showing resource counts and savings
- Grouped recommendations by category for better readability
- Consistent styling and formatting across all services
- Interactive elements for enhanced user experience
- Warnings and permission issues display

Author: AWS Cost Optimization Team
Version: 2.5.9
Last Updated: 2026-01-24
"""

import json
from datetime import datetime
from typing import Dict, Any


class HTMLReportGenerator:
    """
    Professional HTML report generator for AWS cost optimization scan results.

    This class transforms structured JSON scan results into interactive HTML reports
    with professional styling, smart grouping, and zero duplication across services.

    The generator handles:
    - Multi-tab interface creation for 31 AWS services
    - Smart grouping of recommendations by category
    - Deduplication of findings across multiple data sources
    - Consistent styling and formatting
    - Empty tab hiding for clean presentation
    - Statistics calculation and display
    - Profile-based filename generation

    Usage:
        generator = HTMLReportGenerator(scan_results)
        report_path = generator.generate_html_report()
    """

    def __init__(self, scan_results: Dict[str, Any]):
        """
        Initialize the HTML report generator with scan results.

        Args:
            scan_results (Dict[str, Any]): Complete scan results from CostOptimizer.scan_region()
                                         containing services data, statistics, and metadata
        """
        self.scan_results = scan_results

    def generate_html_report(self, output_file: str = None) -> str:
        """
        Generate complete interactive HTML report from scan results.

        Creates a professional HTML report with multi-tab interface, smart grouping,
        and consistent styling. Automatically generates profile-based filename if
        not specified.

        Args:
            output_file (str, optional): Custom output filename. If not provided,
                                       generates filename as 'profile_region.html'

        Returns:
            str: Path to the generated HTML report file

        Note:
            - Automatically hides tabs for services with no recommendations
            - Uses smart grouping for 11 services with high recommendation volumes
            - Applies consistent styling across all service tabs
            - Generates responsive design for desktop and mobile viewing
        """
        if not output_file:
            # Generate profile-based filename for multi-account management
            profile = self.scan_results.get("profile", "default")
            region = self.scan_results["region"]
            output_file = f"{profile}_{region}.html"

        html_content = self._build_html()

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        return output_file

    def _build_html(self) -> str:
        """Build complete HTML content"""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AWS Cost Optimization Report - {self.scan_results["region"]}</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    {self._get_css()}
</head>
<body>
    <button class="theme-toggle" onclick="toggleTheme()" title="Toggle Dark Mode">
        <span id="theme-icon">🌙</span>
        <span id="theme-text">Dark</span>
    </button>
    <div class="container">
        {self._get_header()}
        {self._get_summary()}
        {self._get_tabs()}
        {self._get_footer()}
    </div>
    {self._get_javascript()}
</body>
</html>"""

    def _get_css(self) -> str:
        """Get Material Design CSS styles"""
        return """<style>
        /* Material Design Base */
        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box; 
        }
        
        :root {
            --primary: #1976d2;
            --primary-dark: #0d47a1;
            --primary-light: #42a5f5;
            --secondary: #ff9800;
            --secondary-dark: #f57c00;
            --success: #4caf50;
            --warning: #ff9800;
            --danger: #f44336;
            --info: #2196f3;
            --surface: #ffffff;
            --background: #f5f5f5;
            --text-primary: #212121;
            --text-secondary: #757575;
            --divider: #e0e0e0;
            --shadow-1: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24);
            --shadow-2: 0 3px 6px rgba(0,0,0,0.16), 0 3px 6px rgba(0,0,0,0.23);
            --shadow-3: 0 10px 20px rgba(0,0,0,0.19), 0 6px 6px rgba(0,0,0,0.23);
            --shadow-4: 0 14px 28px rgba(0,0,0,0.25), 0 10px 10px rgba(0,0,0,0.22);
            --shadow-5: 0 19px 38px rgba(0,0,0,0.30), 0 15px 12px rgba(0,0,0,0.22);
        }
        
        [data-theme="dark"] {
            --primary: #42a5f5;
            --primary-dark: #1976d2;
            --primary-light: #64b5f6;
            --secondary: #ffb74d;
            --secondary-dark: #ff9800;
            --success: #66bb6a;
            --warning: #ffb74d;
            --danger: #ef5350;
            --info: #42a5f5;
            --surface: #1e1e1e;
            --background: #121212;
            --text-primary: #ffffff;
            --text-secondary: #b0b0b0;
            --divider: #333333;
            --shadow-1: 0 1px 3px rgba(0,0,0,0.3), 0 1px 2px rgba(0,0,0,0.4);
            --shadow-2: 0 3px 6px rgba(0,0,0,0.4), 0 3px 6px rgba(0,0,0,0.5);
            --shadow-3: 0 10px 20px rgba(0,0,0,0.5), 0 6px 6px rgba(0,0,0,0.6);
            --shadow-4: 0 14px 28px rgba(0,0,0,0.6), 0 10px 10px rgba(0,0,0,0.7);
            --shadow-5: 0 19px 38px rgba(0,0,0,0.7), 0 15px 12px rgba(0,0,0,0.8);
        }
        
        body { 
            font-family: 'Roboto', 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
            line-height: 1.6; 
            color: var(--text-primary);
            background: var(--background);
            min-height: 100vh;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
        }
        
        .container { 
            max-width: 1440px; 
            margin: 0 auto; 
            padding: 24px;
        }
        
        /* Material Header */
        .header { 
            background: linear-gradient(135deg, var(--primary-dark) 0%, var(--primary) 100%);
            color: white; 
            padding: 48px 32px; 
            border-radius: 8px;
            margin-bottom: 24px;
            box-shadow: var(--shadow-3);
            position: relative;
            overflow: hidden;
        }
        
        .header::before {
            content: '';
            position: absolute;
            top: -50%;
            right: -10%;
            width: 500px;
            height: 500px;
            background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
            border-radius: 50%;
        }
        
        .header h1 { 
            font-size: 2.75rem;
            font-weight: 400;
            margin-bottom: 8px;
            position: relative;
            z-index: 1;
            letter-spacing: -0.5px;
        }
        
        .header .subtitle { 
            font-size: 1.25rem;
            opacity: 0.9;
            font-weight: 300;
            position: relative;
            z-index: 1;
        }
        
        .header-info { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); 
            gap: 16px; 
            margin-top: 32px;
            position: relative;
            z-index: 1;
        }
        
        .header-info-item {
            background: rgba(255,255,255,0.15);
            padding: 16px;
            border-radius: 8px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.2);
        }
        
        .header-info-item strong {
            display: block;
            font-size: 0.875rem;
            opacity: 0.8;
            margin-bottom: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
        }
        
        /* Material Summary Cards */
        .summary {
            margin-bottom: 24px;
        }
        
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
        }
        
        .summary-card {
            background: var(--surface);
            padding: 24px;
            border-radius: 8px;
            box-shadow: var(--shadow-2);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            position: relative;
            overflow: hidden;
        }
        
        .summary-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--primary);
            transform: scaleY(0);
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        .summary-card:hover {
            box-shadow: var(--shadow-4);
            transform: translateY(-4px);
        }
        
        .summary-card:hover::before {
            transform: scaleY(1);
        }
        
        .summary-card h3 { 
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-secondary);
        }
        
        .summary-card .value { 
            font-size: 2.5rem;
            font-weight: 400;
            color: var(--text-primary);
            line-height: 1;
        }
        
        .summary-card .subtitle {
            font-size: 0.875rem;
            color: var(--text-secondary);
            margin-top: 8px;
        }
        
        /* Material Tabs */
        .tabs {
            background: var(--surface);
            border-radius: 8px;
            box-shadow: var(--shadow-2);
            overflow: hidden;
            margin-bottom: 24px;
        }
        
        .tab-buttons {
            display: flex;
            background: var(--surface);
            border-bottom: 1px solid var(--divider);
            overflow-x: auto;
            scrollbar-width: thin;
        }
        
        .tab-buttons::-webkit-scrollbar {
            height: 4px;
        }
        
        .tab-buttons::-webkit-scrollbar-track {
            background: var(--background);
        }
        
        .tab-buttons::-webkit-scrollbar-thumb {
            background: var(--primary);
            border-radius: 4px;
        }
        
        .tab-button {
            flex: 0 0 auto;
            min-width: 160px;
            padding: 16px 24px;
            background: transparent;
            border: none;
            cursor: pointer;
            font-weight: 500;
            font-size: 0.875rem;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            color: var(--text-secondary);
            text-align: center;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid transparent;
            position: relative;
        }
        
        .tab-button::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: var(--primary);
            transform: scaleX(0);
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        .tab-button:hover {
            background: rgba(0,0,0,0.04);
            color: var(--text-primary);
        }
        
        .tab-button.active {
            color: var(--primary);
            font-weight: 600;
        }
        
        .tab-button.active::after {
            transform: scaleX(1);
        }
        
        .tab-content {
            display: none;
            padding: 32px;
            animation: fadeIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        .tab-content.active { 
            display: block;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        /* Service Header */
        .service-header {
            margin-bottom: 32px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--divider);
        }
        
        .service-title {
            font-size: 2rem;
            font-weight: 400;
            color: var(--text-primary);
            margin-bottom: 24px;
            letter-spacing: -0.5px;
        }
        
        .service-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
        }
        
        .stat-card {
            background: var(--background);
            padding: 16px;
            border-radius: 8px;
            text-align: center;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border: 1px solid var(--divider);
        }
        
        .stat-card:hover {
            background: var(--surface);
            box-shadow: var(--shadow-1);
            transform: translateY(-2px);
        }
        
        .stat-card h4 {
            font-size: 0.75rem;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
        }
        
        .stat-card .value {
            font-size: 1.75rem;
            font-weight: 400;
            color: var(--text-primary);
        }
        
        /* Value status colors */
        .value.success, .success {
            color: var(--success);
            font-weight: 500;
        }
        
        .value.warning, .warning {
            color: var(--warning);
            font-weight: 500;
        }
        
        .value.danger, .danger {
            color: var(--danger);
            font-weight: 500;
        }
        
        /* Source info and callout styling */
        .source-info {
            font-size: 0.875rem;
            color: var(--text-secondary);
        }
        
        .callout-margin {
            margin-top: 16px;
        }
        
        .stat-card.savings {
            background: rgba(76, 175, 80, 0.1);
            border: 1px solid rgba(76, 175, 80, 0.3);
        }
        
        .stat-card.savings .value {
            color: var(--success);
        }
        
        /* Section title for recommendations */
        .section-title {
            font-size: 1.25rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid var(--divider);
        }
        
        /* Savings highlight - works inside and outside rec-item */
        .savings {
            color: var(--success);
            font-weight: 500;
        }
        
        .rec-summary .savings {
            background: rgba(76, 175, 80, 0.1);
            padding: 4px 8px;
            border-radius: 4px;
        }
        
        /* Material Recommendation Cards */
        .recommendation-list {
            margin-top: 24px;
        }
        
        .rec-item {
            background: var(--surface);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
            box-shadow: var(--shadow-1);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border-left: 4px solid var(--primary);
        }
        
        .rec-item:hover {
            box-shadow: var(--shadow-3);
            transform: translateX(4px);
        }
        
        .rec-item.high-priority {
            border-left-color: var(--danger);
        }
        
        .rec-item.medium-priority {
            border-left-color: var(--warning);
        }
        
        .rec-item.low-priority {
            border-left-color: var(--success);
        }
        
        .rec-item h5 {
            font-size: 1.125rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 12px;
        }
        
        .rec-item p {
            margin-bottom: 12px;
            color: var(--text-secondary);
            line-height: 1.7;
        }
        
        .rec-item strong {
            color: var(--text-primary);
            font-weight: 500;
        }
        
        .rec-item .savings { 
            color: var(--success);
            font-weight: 500;
            background: rgba(76, 175, 80, 0.1);
            padding: 8px 16px;
            border-radius: 4px;
            display: inline-block;
            margin: 8px 0;
            font-size: 0.875rem;
        }
        
        /* Material Chips/Badges */
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 4px 12px;
            font-size: 0.75rem;
            font-weight: 500;
            text-align: center;
            white-space: nowrap;
            border-radius: 16px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .badge-warning {
            color: #e65100;
            background-color: #fff3e0;
        }
        
        .badge-info {
            color: #01579b;
            background-color: #e1f5fe;
        }
        
        .badge-success {
            color: #1b5e20;
            background-color: #e8f5e9;
        }
        
        .badge-danger {
            color: #b71c1c;
            background-color: #ffebee;
        }
        
        /* Material Tables */
        .recommendations-table,
        .top-buckets-table table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 16px;
            background: var(--surface);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: var(--shadow-1);
        }
        
        .recommendations-table th,
        .top-buckets-table th {
            background: var(--background);
            padding: 16px;
            text-align: left;
            font-weight: 500;
            font-size: 0.875rem;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid var(--divider);
        }
        
        .recommendations-table td,
        .top-buckets-table td {
            padding: 16px;
            border-bottom: 1px solid var(--divider);
            vertical-align: top;
            color: var(--text-primary);
        }
        
        .recommendations-table tr:hover,
        .top-buckets-table tr:hover {
            background-color: rgba(0,0,0,0.02);
        }
        
        .recommendations-table tr:last-child td,
        .top-buckets-table tr:last-child td {
            border-bottom: none;
        }
        
        .recommendations-table code {
            background: var(--background);
            padding: 2px 8px;
            border-radius: 4px;
            font-family: 'Roboto Mono', 'Courier New', monospace;
            font-size: 0.875rem;
            color: var(--primary);
        }
        
        /* Material Success/Info Boxes */
        .success {
            color: var(--success);
            font-weight: 500;
            background: rgba(76, 175, 80, 0.1);
            padding: 16px;
            border-radius: 8px;
            border-left: 4px solid var(--success);
            margin: 16px 0;
        }
        
        .info-box {
            background: rgba(33, 150, 243, 0.1);
            padding: 16px;
            border-radius: 8px;
            border-left: 4px solid var(--info);
            margin: 16px 0;
            color: var(--text-primary);
        }
        
        .warning-box {
            background: rgba(255, 152, 0, 0.1);
            padding: 16px;
            border-radius: 8px;
            border-left: 4px solid var(--warning);
            margin: 16px 0;
            color: var(--text-primary);
        }
        
        /* Material Sections */
        .top-buckets-table {
            margin: 24px 0;
            background: var(--surface);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: var(--shadow-2);
        }
        
        .top-buckets-table h4 {
            background: var(--primary);
            color: white;
            padding: 20px 24px;
            margin: 0;
            font-size: 1.125rem;
            font-weight: 500;
        }
        
        .affected-resources {
            margin: 24px 0;
            padding: 24px;
            background: var(--surface);
            border-radius: 8px;
            border-left: 4px solid var(--primary);
            box-shadow: var(--shadow-1);
        }
        
        .resource-group {
            margin-bottom: 16px;
            padding: 16px;
            background: var(--background);
            border-radius: 8px;
            border: 1px solid var(--divider);
        }
        
        .resource-group h5 {
            color: var(--primary);
            margin-bottom: 8px;
            font-weight: 500;
            font-size: 1rem;
        }
        
        .group-savings {
            color: var(--success);
            font-weight: 500;
            margin-bottom: 8px;
        }
        
        .resource-list {
            margin-left: 20px;
            color: var(--text-secondary);
        }
        
        .resource-list li {
            margin-bottom: 4px;
            line-height: 1.6;
        }
        
        .show-more-link {
            color: var(--primary);
            text-decoration: none;
            font-weight: 500;
            cursor: pointer;
            transition: color 0.2s cubic-bezier(0.4, 0.0, 0.2, 1);
        }
        
        .show-more-link:hover {
            color: var(--primary-dark);
            text-decoration: underline;
        }
        
        .show-more-container {
            margin: 16px 0;
            text-align: center;
        }
        
        .rec-summary {
            background: rgba(255, 152, 0, 0.1);
            padding: 16px 24px;
            border-radius: 8px;
            margin-bottom: 24px;
            border-left: 4px solid var(--warning);
            font-size: 1rem;
            color: var(--text-primary);
        }
        
        .opportunities {
            margin-top: 24px;
        }
        
        .opportunity {
            background: rgba(76, 175, 80, 0.1);
            padding: 16px;
            margin: 12px 0;
            border-radius: 8px;
            border-left: 4px solid var(--success);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        
        .opportunity:hover {
            transform: translateX(4px);
            box-shadow: var(--shadow-1);
        }
        
        /* Material Footer */
        .footer {
            margin-top: 48px;
            padding: 32px;
            background: var(--surface);
            border-radius: 8px;
            text-align: center;
            color: var(--text-secondary);
            border-top: 1px solid var(--divider);
            box-shadow: var(--shadow-1);
        }
        
        .footer p {
            margin: 8px 0;
            font-size: 0.875rem;
        }
        
        /* Responsive Design */
        @media (max-width: 960px) {
            .container { padding: 16px; }
            .header { padding: 32px 24px; }
            .header h1 { font-size: 2rem; }
            .summary-grid { grid-template-columns: 1fr; }
            .service-stats { grid-template-columns: repeat(2, 1fr); }
        }
        
        @media (max-width: 600px) {
            .header h1 { font-size: 1.75rem; }
            .header .subtitle { font-size: 1rem; }
            .tab-button { min-width: 120px; font-size: 0.75rem; padding: 12px 16px; }
            .service-title { font-size: 1.5rem; }
            .service-stats { grid-template-columns: 1fr; }
            .summary-card .value { font-size: 2rem; }
            .stat-card .value { font-size: 1.5rem; }
            .rec-item { padding: 16px; }
            .top-buckets-table { overflow-x: auto; }
        }
        
        /* Charts Container */
        .charts-container {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 32px;
            margin-top: 32px;
        }
        
        .chart-section {
            background: var(--surface);
            border-radius: 8px;
            padding: 24px;
            box-shadow: var(--shadow-1);
        }
        
        .chart-section h3 {
            font-size: 1.125rem;
            color: var(--text-primary);
            margin-bottom: 16px;
            text-align: center;
        }
        
        .chart-wrapper {
            position: relative;
            height: 400px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        @media (max-width: 768px) {
            .charts-container {
                grid-template-columns: 1fr;
            }
        }
        
        /* Dark Mode Toggle */
        .theme-toggle {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 1000;
            background: var(--surface);
            border: 1px solid var(--divider);
            border-radius: 50px;
            padding: 8px 16px;
            cursor: pointer;
            box-shadow: var(--shadow-2);
            transition: all 0.3s ease;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            color: var(--text-primary);
        }

        .theme-toggle:hover {
            box-shadow: var(--shadow-3);
            transform: translateY(-1px);
        }
        
        /* Print Styles */
        @media print {
            body { background: white; }
            .container { box-shadow: none; }
            .tab-buttons { display: none; }
            .tab-content { display: block !important; page-break-inside: avoid; }
            .rec-item { page-break-inside: avoid; }
        }
        </style>"""

    def _get_header(self) -> str:
        """Get header section"""
        return f"""<div class="header">
            <h1>AWS Cost Optimization Report</h1>
            <div class="header-info">
                <div><strong>Account ID:</strong> {self.scan_results["account_id"]}</div>
                <div><strong>Region:</strong> {self.scan_results["region"]}</div>
                <div><strong>Scan Time:</strong> {self.scan_results["scan_time"][:19]}</div>
                <div><strong>Services:</strong> {self.scan_results["summary"]["total_services_scanned"]}</div>
            </div>
        </div>"""

    def _get_summary(self) -> str:
        """Get summary section"""
        # Filter out Graviton recommendations from summary
        filtered_services = {}
        total_recommendations = 0
        total_savings = 0
        services_with_recommendations = 0

        for service_key, service_data in self.scan_results["services"].items():
            filtered_data = self._filter_recommendations(service_data)
            if filtered_data["total_recommendations"] > 0:
                services_with_recommendations += 1
            filtered_services[service_key] = filtered_data
            total_recommendations += filtered_data["total_recommendations"]
            total_savings += filtered_data["total_monthly_savings"]

        return f"""<div class="summary">
            <h2>Executive Summary</h2>
            <div class="summary-grid">
                <div class="summary-card">
                    <h3>Total Recommendations</h3>
                    <div class="value">{total_recommendations}</div>
                </div>
                <div class="summary-card">
                    <h3>Estimated Monthly Savings</h3>
                    <div class="value">${total_savings:.2f}</div>
                </div>
                <div class="summary-card">
                    <h3>Services Scanned</h3>
                    <div class="value">{services_with_recommendations}</div>
                </div>
                <div class="summary-card">
                    <h3>Potential Annual Savings</h3>
                    <div class="value">${total_savings * 12:.2f}</div>
                </div>
            </div>
        </div>"""

    def _get_tabs(self) -> str:
        """Get tabs section"""
        services = self.scan_results["services"]

        # Extract snapshots and AMIs from EBS enhanced checks
        snapshots_data = self._extract_snapshots_data(services)
        amis_data = self._extract_amis_data(services)

        # Tab buttons
        tab_buttons = '<div class="tab-buttons">'

        # Add Executive Summary tab first (always active)
        tab_buttons += (
            '<button class="tab-button active" onclick="showTab(\'executive-summary\')">📊 Executive Summary</button>'
        )

        # Add main service tabs with Snapshots and AMIs
        for i, (service_key, service_data) in enumerate(services.items()):
            # Skip tabs with no recommendations
            if service_data.get("total_recommendations", 0) == 0:
                continue

            # No longer first tab since Executive Summary is first
            tab_buttons += f'<button class="tab-button" onclick="showTab(\'{service_key}\')">{service_data["service_name"]}</button>'

            # Add Snapshots tab right after EBS (if snapshots exist)
            if service_key == "ebs" and snapshots_data["count"] > 0:
                tab_buttons += f'<button class="tab-button" onclick="showTab(\'snapshots\')">Snapshots</button>'

        # Add standalone Snapshots tab if no EBS tab but snapshots exist
        if snapshots_data["count"] > 0 and not any(
            s.get("total_recommendations", 0) > 0 for k, s in services.items() if k == "ebs"
        ):
            tab_buttons += f'<button class="tab-button" onclick="showTab(\'snapshots\')">Snapshots</button>'

        # Add standalone AMIs tab if no AMI service but AMIs exist
        if amis_data["count"] > 0 and not any(
            s.get("total_recommendations", 0) > 0 for k, s in services.items() if k == "ami"
        ):
            tab_buttons += f'<button class="tab-button" onclick="showTab(\'amis\')">AMIs</button>'

        tab_buttons += "</div>"

        # Tab contents
        tab_contents = ""

        # Add Executive Summary tab content first (always active)
        tab_contents += '<div id="executive-summary" class="tab-content active">'
        tab_contents += self._get_executive_summary_content()
        tab_contents += "</div>"

        # Add main service tabs with Snapshots and AMIs
        for i, (service_key, service_data) in enumerate(services.items()):
            # Skip tabs with no recommendations
            if service_data.get("total_recommendations", 0) == 0:
                continue

            # No longer active since Executive Summary is active
            tab_contents += f'<div id="{service_key}" class="tab-content">'

            # Use custom AMI content if AMI service
            if service_key == "ami" and amis_data["count"] > 0:
                tab_contents += self._get_amis_content(amis_data)
            else:
                tab_contents += self._get_service_content(service_key, service_data)

            tab_contents += "</div>"

            # Add Snapshots tab content right after EBS
            if service_key == "ebs" and snapshots_data["count"] > 0:
                tab_contents += f'<div id="snapshots" class="tab-content">'
                tab_contents += self._get_snapshots_content(snapshots_data)
                tab_contents += "</div>"

        # Add standalone Snapshots tab if no EBS tab but snapshots exist
        if snapshots_data["count"] > 0 and not any(
            s.get("total_recommendations", 0) > 0 for k, s in services.items() if k == "ebs"
        ):
            tab_contents += f'<div id="snapshots" class="tab-content">'
            tab_contents += self._get_snapshots_content(snapshots_data)
            tab_contents += "</div>"

        # Add standalone AMIs tab if no AMI service but AMIs exist
        if amis_data["count"] > 0 and not any(
            s.get("total_recommendations", 0) > 0 for k, s in services.items() if k == "ami"
        ):
            tab_contents += f'<div id="amis" class="tab-content">'
            tab_contents += self._get_amis_content(amis_data)
            tab_contents += "</div>"

        return f'<div class="tabs">{tab_buttons}{tab_contents}</div>'

    def _extract_snapshots_data(self, services: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all snapshot-related recommendations from services"""
        snapshots = []
        seen_snapshot_ids = set()

        # Check EBS enhanced checks
        ebs_service = services.get("ebs", {})
        ebs_sources = ebs_service.get("sources", {})
        enhanced_checks = ebs_sources.get("enhanced_checks", {})

        for rec in enhanced_checks.get("recommendations", []):
            check_category = rec.get("CheckCategory", "")
            if "snapshot" in check_category.lower():
                # Deduplicate by SnapshotId
                snapshot_id = rec.get("SnapshotId", "N/A")
                if snapshot_id != "N/A" and snapshot_id not in seen_snapshot_ids:
                    seen_snapshot_ids.add(snapshot_id)
                    snapshots.append(rec)

        return {
            "count": len(snapshots),
            "recommendations": snapshots,
            "total_savings": sum(
                float(rec.get("EstimatedSavings", "0").replace("$", "").replace("/month", "").split("(")[0].strip())
                for rec in snapshots
                if "EstimatedSavings" in rec
                and rec.get("EstimatedSavings", "0") != "0"
                and rec.get("EstimatedSavings", "")
                .replace("$", "")
                .replace("/month", "")
                .split("(")[0]
                .strip()
                .replace(".", "")
                .isdigit()
            ),
        }

    def _extract_amis_data(self, services: Dict[str, Any]) -> Dict[str, Any]:
        """Extract all AMI-related recommendations from services"""
        amis = []

        # Check dedicated AMI service section
        ami_service = services.get("ami", {})
        ami_sources = ami_service.get("sources", {})

        for source_name, source_data in ami_sources.items():
            if isinstance(source_data, dict):
                amis.extend(source_data.get("recommendations", []))
            elif isinstance(source_data, list):
                amis.extend(source_data)

        # Also check EC2 enhanced checks for backward compatibility
        ec2_service = services.get("ec2", {})
        ec2_sources = ec2_service.get("sources", {})
        enhanced_checks = ec2_sources.get("enhanced_checks", {})

        for rec in enhanced_checks.get("recommendations", []):
            check_category = rec.get("CheckCategory", "")
            if "ami" in check_category.lower():
                amis.append(rec)

        return {"count": len(amis), "recommendations": amis}

    def _get_snapshots_content(self, snapshots_data: Dict[str, Any]) -> str:
        """Generate content for Snapshots tab"""
        # Group snapshots by age range
        age_groups = {"90-180 days": [], "180-365 days": [], "1-2 years": [], "2+ years": []}

        total_savings = 0
        for rec in snapshots_data["recommendations"]:
            # Filter out invalid entries
            snapshot_id = rec.get("SnapshotId", "N/A")
            age_days = rec.get("AgeDays", 0)

            # Skip entries with missing SnapshotId or invalid age
            if snapshot_id == "N/A" or age_days < 90:
                continue

            if age_days <= 180:
                age_groups["90-180 days"].append(rec)
            elif age_days <= 365:
                age_groups["180-365 days"].append(rec)
            elif age_days <= 730:
                age_groups["1-2 years"].append(rec)
            else:
                age_groups["2+ years"].append(rec)

            # Calculate savings
            savings_str = rec.get("EstimatedSavings", "$0/month")
            if "$" in savings_str and "/month" in savings_str:
                try:
                    # Remove currency, time unit, and any additional text like "(max estimate)"
                    clean_str = savings_str.replace("$", "").replace("/month", "").split("(")[0].strip()
                    savings_val = float(clean_str)
                    total_savings += savings_val
                except (ValueError, AttributeError) as e:
                    print(f"⚠️ Could not parse snapshot savings '{savings_str}': {str(e)}")
                    # Continue without this savings amount

        # Use standard service header format
        content = '<div class="service-header">'
        content += '<h2 class="service-title">Snapshots Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += (
            f'<div class="stat-card"><h4>Old Snapshots</h4><div class="value">{snapshots_data["count"]}</div></div>'
        )
        content += f'<div class="stat-card"><h4>Potential Monthly Savings</h4><div class="value savings">${total_savings:.2f}</div></div>'
        content += "</div></div>"

        # Use standard recommendations section format
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title">💡 Optimization Recommendations</h3>'
        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {snapshots_data["count"]} | '
        content += (
            f'<strong>Estimated Monthly Savings:</strong> <span class="savings">${total_savings:.2f}</span></div>'
        )

        content += '<div class="recommendation-list">'

        for age_range, snapshots in age_groups.items():
            if not snapshots:
                continue

            group_savings = 0
            total_size = 0
            for snap in snapshots:
                savings_str = snap.get("EstimatedSavings", "$0/month")
                if "$" in savings_str and "/month" in savings_str:
                    try:
                        # Remove currency, time unit, and any additional text like "(max estimate)"
                        clean_str = savings_str.replace("$", "").replace("/month", "").split("(")[0].strip()
                        group_savings += float(clean_str)
                    except (ValueError, AttributeError) as e:
                        print(f"⚠️ Could not parse snapshot savings '{savings_str}': {str(e)}")
                        # Continue without this savings amount
                total_size += snap.get("VolumeSize", 0)

            content += f'<div class="rec-item">'
            content += f"<h5>Snapshots aged {age_range} ({len(snapshots)} snapshots, {total_size} GB total)</h5>"
            content += (
                f"<p><strong>Recommendation:</strong> Review and delete old snapshots that are no longer needed</p>"
            )
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${group_savings:.2f}/month</p>'
            content += "<p><strong>Snapshots:</strong></p><ul>"

            for snap in snapshots:
                snapshot_id = snap.get("SnapshotId", "N/A")
                age_days = snap.get("AgeDays", 0)
                volume_size = snap.get("VolumeSize", 0)
                savings = snap.get("EstimatedSavings", "N/A")
                content += f"<li>{snapshot_id} - {age_days} days old, {volume_size} GB ({savings})</li>"

            content += "</ul></div>"

        content += "</div></div>"
        return content

    def _get_amis_content(self, amis_data: Dict[str, Any]) -> str:
        """Generate content for AMIs tab"""
        # Group AMIs by age range
        age_groups = {"90-180 days": [], "180-365 days": [], "1-2 years": [], "2+ years": []}

        for rec in amis_data["recommendations"]:
            age_days = rec.get("AgeDays", 0)
            if age_days <= 180:
                age_groups["90-180 days"].append(rec)
            elif age_days <= 365:
                age_groups["180-365 days"].append(rec)
            elif age_days <= 730:
                age_groups["1-2 years"].append(rec)
            else:
                age_groups["2+ years"].append(rec)

        # Use standard service header format
        content = '<div class="service-header">'
        content += '<h2 class="service-title">AMI Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += f'<div class="stat-card"><h4>Old AMIs</h4><div class="value">{amis_data["count"]}</div></div>'
        content += "</div></div>"

        # Use standard recommendations section format
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title">💡 Optimization Recommendations</h3>'

        # Calculate total savings
        total_savings = sum(ami.get("EstimatedMonthlySavings", 0) for amis in age_groups.values() for ami in amis)

        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {amis_data["count"]} | '
        content += (
            f'<strong>Estimated Monthly Savings:</strong> <span class="savings">${total_savings:.2f}</span></div>'
        )

        content += '<div class="recommendation-list">'

        for age_range, amis in age_groups.items():
            if not amis:
                continue

            # Calculate savings for this age group
            group_savings = sum(ami.get("EstimatedMonthlySavings", 0) for ami in amis)

            content += f'<div class="rec-item">'
            content += f"<h5>AMIs aged {age_range} ({len(amis)} images)</h5>"
            content += f"<p><strong>Recommendation:</strong> Review and deregister unused AMIs to eliminate snapshot storage costs</p>"
            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${group_savings:.2f}/month</p>'
            content += "<p><strong>AMIs:</strong></p><ul>"

            for ami in amis:
                ami_id = ami.get("ImageId", "N/A")
                ami_name = ami.get("Name", "Unnamed")
                age_days = ami.get("AgeDays", 0)
                ami_savings = ami.get("EstimatedSavings", "N/A")
                content += f"<li>{ami_id} - {ami_name} ({age_days} days old) - {ami_savings}</li>"

            content += "</ul></div>"

        content += "</div></div>"
        return content

    def _get_executive_summary_content(self) -> str:
        """Generate executive summary content with charts and key metrics"""
        services = self.scan_results["services"]
        summary = self.scan_results.get("summary", {})

        # Basic validation - check if we have recommendations
        total_recommendations = summary.get("total_recommendations", 0)
        if total_recommendations == 0:
            return """
            <div class="service-header">
                <h2 class="service-title">📊 Executive Summary</h2>
            </div>
            <div class="empty-state">
                <h3>No Cost Optimization Recommendations Found</h3>
                <p>Your AWS resources appear to be well-optimized. No immediate cost savings opportunities were identified.</p>
            </div>
            """

        # Key metrics
        total_savings = summary.get("total_monthly_savings", 0)
        services_scanned = summary.get("total_services_scanned", 0)

        content = (
            """
        <div class="service-header">
            <h2 class="service-title">📊 Executive Summary</h2>
            <div class="service-stats">
                <div class="stat-card">
                    <h4>Total Monthly Savings</h4>
                    <div class="value savings">$"""
            + f"{total_savings:.2f}"
            + """</div>
                </div>
                <div class="stat-card">
                    <h4>Total Recommendations</h4>
                    <div class="value">"""
            + str(total_recommendations)
            + """</div>
                </div>
                <div class="stat-card">
                    <h4>Services Scanned</h4>
                    <div class="value">"""
            + str(services_scanned)
            + """</div>
                </div>
            </div>
        </div>
        
        <div class="charts-container">
            <div class="chart-section">
                <h3>Cost Savings Distribution by Service</h3>
                <div class="chart-wrapper">
                    <canvas id="savingsPieChart" width="400" height="400"></canvas>
                </div>
            </div>
            <div class="chart-section">
                <h3>Top Services by Savings Potential</h3>
                <div class="chart-wrapper">
                    <canvas id="savingsBarChart" width="400" height="400"></canvas>
                </div>
            </div>
        </div>
        """
        )

        return content

    def _filter_recommendations(self, service_data: Dict[str, Any]) -> Dict[str, Any]:
        """Filter out non-relevant recommendations like MigrateToGraviton"""
        filtered_data = service_data.copy()

        if "sources" in filtered_data:
            for source_name, source_data in filtered_data["sources"].items():
                if "recommendations" in source_data:
                    # Filter out MigrateToGraviton recommendations
                    original_recs = source_data["recommendations"]
                    filtered_recs = [
                        rec
                        for rec in original_recs
                        if isinstance(rec, dict) and rec.get("actionType") != "MigrateToGraviton"
                    ]

                    # Update counts and savings
                    filtered_data["sources"][source_name]["recommendations"] = filtered_recs
                    filtered_data["sources"][source_name]["count"] = len(filtered_recs)

                    # Recalculate total recommendations and savings
                    if source_name == "cost_optimization_hub":
                        graviton_savings = sum(
                            rec.get("estimatedMonthlySavings", 0)
                            for rec in original_recs
                            if isinstance(rec, dict) and rec.get("actionType") == "MigrateToGraviton"
                        )
                        filtered_data["total_monthly_savings"] = max(
                            0, filtered_data.get("total_monthly_savings", 0) - graviton_savings
                        )

        # Recalculate total recommendations and savings
        total_recs = 0
        for source in filtered_data.get("sources", {}).values():
            if isinstance(source, dict):
                # Old format: {'count': X, 'recommendations': [...]}
                total_recs += source.get("count", 0)
            elif isinstance(source, list):
                # New format: direct list of recommendations
                total_recs += len(source)
        filtered_data["total_recommendations"] = total_recs

        # For services without direct savings (like Compute Optimizer), preserve calculated savings
        if service_data.get("service_name") == "EC2" and all(
            "actionType" not in rec
            for source in filtered_data.get("sources", {}).values()
            for rec in (source.get("recommendations", []) if isinstance(source, dict) else source)
        ):
            # Only set to 0 if no calculated savings were provided
            if filtered_data.get("total_monthly_savings", 0) == 0:
                filtered_data["total_monthly_savings"] = 0

        return filtered_data

    def _get_affected_resources_list(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get list of affected resources for each recommendation type"""
        content = ""
        sources = service_data.get("sources", {})

        # Collect resources by recommendation type
        resource_groups = {}

        for source_name, source_data in sources.items():
            # Handle both old format (dict with 'recommendations') and new format (direct list)
            if isinstance(source_data, dict):
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                recommendations = source_data
            else:
                recommendations = []

            for rec in recommendations:
                if service_key == "ec2":
                    # Handle Cost Optimization Hub recommendations
                    if "actionType" in rec:
                        # Skip EBS volumes in EC2 section
                        if "ebsVolume" in rec.get("currentResourceDetails", {}):
                            continue

                        # Skip Reserved Instances recommendations (they're for RDS/ElastiCache)
                        if rec.get("actionType") == "PurchaseReservedInstances":
                            continue

                        # Skip ECS resources in EC2 section
                        resource_details = rec.get("currentResourceDetails", {})
                        if "ecsService" in resource_details or "ecsCluster" in resource_details:
                            continue

                        # Additional ECS filtering by resource ID pattern - comprehensive check
                        resource_id = rec.get("resourceId", "N/A")
                        resource_name = rec.get("Name", rec.get("ResourceName", ""))
                        all_text = f"{resource_id} {resource_name}".lower()

                        if (
                            "ecs-cluster" in all_text
                            or "/cronjob" in all_text
                            or "forwarder" in all_text
                            or "lambda" in all_text
                            or "ecs" in all_text
                        ):
                            continue

                        # Only include actual EC2 instances
                        if "ec2Instance" not in resource_details:
                            continue

                        action_type = rec.get("actionType", "Unknown")
                        resource_id = rec.get("resourceId", "N/A")

                        # Only process if we have a valid resource ID
                        if resource_id == "N/A":
                            continue

                        instance_type = (
                            rec.get("currentResourceDetails", {})
                            .get("ec2Instance", {})
                            .get("configuration", {})
                            .get("instance", {})
                            .get("type", "N/A")
                        )
                        savings = rec.get("estimatedMonthlySavings", 0)

                        if action_type not in resource_groups:
                            resource_groups[action_type] = []
                        resource_groups[action_type].append(
                            {"id": resource_id, "type": instance_type, "savings": savings}
                        )

                    # Handle Compute Optimizer recommendations
                    elif "instanceArn" in rec:
                        finding = rec.get("finding", "Unknown")

                        # Skip optimized resources
                        if finding.lower() in ["optimized", "over_provisioned"]:
                            continue

                        instance_name = rec.get("instanceName", "N/A")
                        instance_id = rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
                        current_type = rec.get("currentInstanceType", "N/A")

                        # Get recommended instance type
                        recommended_type = "N/A"
                        if rec.get("recommendationOptions"):
                            recommended_type = rec["recommendationOptions"][0].get("instanceType", "N/A")

                        group_name = f"Rightsizing - {finding}"
                        if group_name not in resource_groups:
                            resource_groups[group_name] = []
                        resource_groups[group_name].append(
                            {
                                "id": instance_name or instance_id,
                                "type": f"{current_type} → {recommended_type}",
                                "savings": 0,  # Compute Optimizer doesn't provide direct savings
                            }
                        )

                elif service_key == "ebs":
                    # Handle Cost Optimization Hub EBS recommendations
                    if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
                        action_type = rec.get("actionType", "Unknown")
                        resource_id = rec.get("resourceId", "N/A")

                        # Extract volume configuration
                        ebs_config = rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
                        volume_type = ebs_config.get("storage", {}).get("type", "N/A")
                        volume_size = ebs_config.get("storage", {}).get("sizeInGb", 0)

                        savings = rec.get("estimatedMonthlySavings", 0)

                        if action_type not in resource_groups:
                            resource_groups[action_type] = []
                        resource_groups[action_type].append(
                            {"id": resource_id, "type": f"{volume_type} ({volume_size} GB)", "savings": savings}
                        )
                    # Check for gp2 migration recommendations
                    elif rec.get("CheckCategory") == "Volume Type Optimization" and rec.get("CurrentType") == "gp2":
                        if "gp2 to gp3 Migration" not in resource_groups:
                            resource_groups["gp2 to gp3 Migration"] = []
                        resource_groups["gp2 to gp3 Migration"].append(
                            {
                                "id": rec.get("VolumeId", "N/A"),
                                "type": f"{rec.get('Size', 0)} GB (20% savings)",
                                "savings": 0,  # Percentage-based, not dollar amount
                            }
                        )
                    # Unattached volumes - only from unattached_volumes source
                    elif source_name == "unattached_volumes" and "VolumeId" in rec:
                        if "Unattached Volumes" not in resource_groups:
                            resource_groups["Unattached Volumes"] = []
                        resource_groups["Unattached Volumes"].append(
                            {
                                "id": rec.get("VolumeId", "N/A"),
                                "type": f"{rec.get('VolumeType', 'N/A')} ({rec.get('Size', 0)} GB)",
                                "savings": rec.get("EstimatedMonthlyCost", 0),
                            }
                        )
                    elif rec.get("finding") == "NotOptimized":  # Compute Optimizer recommendation
                        if "Volume Optimization" not in resource_groups:
                            resource_groups["Volume Optimization"] = []
                        volume_id = rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
                        resource_groups["Volume Optimization"].append(
                            {
                                "id": volume_id,
                                "type": rec.get("finding", "N/A"),
                                "savings": 0,  # Compute Optimizer doesn't provide direct savings
                            }
                        )
                    # Skip optimized EBS volumes from Compute Optimizer
                    elif rec.get("finding", "").lower() == "optimized":
                        continue

                elif service_key == "rds":
                    # Try multiple field names for finding - be more flexible
                    finding = (
                        rec.get("instanceFinding")
                        or rec.get("InstanceFinding")
                        or rec.get("finding")
                        or rec.get("CheckCategory")
                        or rec.get("Recommendation")
                        or "Optimization Opportunity"
                    )

                    # Skip optimized and underprovisioned RDS instances
                    if finding.lower() == "optimized" or finding.lower() == "underprovisioned":
                        continue

                    # Extract database name
                    resource_arn = rec.get("resourceArn") or rec.get("ResourceArn", "N/A")
                    if resource_arn != "N/A":
                        db_name = resource_arn.split(":")[-1]
                    else:
                        db_name = rec.get("DBInstanceIdentifier") or rec.get("Database") or rec.get("resourceId", "N/A")

                    # Extract engine - try multiple fields
                    engine = rec.get("engine") or rec.get("Engine") or rec.get("engineVersion") or "Unknown"

                    # Skip snapshots from Compute Optimizer grouping (they're handled in enhanced_checks)
                    if "SnapshotId" in rec or "snapshot" in db_name.lower():
                        continue

                    # Categorize by Aurora Cluster vs Standalone
                    if "aurora" in engine.lower():
                        category = f"Aurora Clusters - {finding}"
                    else:
                        category = f"Standalone Instances - {finding}"

                    if category not in resource_groups:
                        resource_groups[category] = []
                    resource_groups[category].append(
                        {
                            "id": db_name,
                            "type": engine,
                            "savings": 0,  # RDS Compute Optimizer doesn't provide direct savings
                        }
                    )

                elif service_key == "file_systems":
                    if "FileSystemType" in rec:  # FSx
                        fs_type = rec.get("FileSystemType", "Unknown")
                        if f"FSx {fs_type}" not in resource_groups:
                            resource_groups[f"FSx {fs_type}"] = []
                        resource_groups[f"FSx {fs_type}"].append(
                            {
                                "id": rec.get("FileSystemId", "N/A"),
                                "type": f"{rec.get('StorageCapacity', 0)} GB",
                                "savings": rec.get("EstimatedMonthlyCost", 0) * 0.3,  # Assume 30% savings potential
                            }
                        )
                    else:  # EFS
                        if not rec.get("HasIAPolicy", True):  # Missing IA policy
                            if "EFS Lifecycle Optimization" not in resource_groups:
                                resource_groups["EFS Lifecycle Optimization"] = []
                            resource_groups["EFS Lifecycle Optimization"].append(
                                {
                                    "id": rec.get("Name", rec.get("FileSystemId", "N/A")),
                                    "type": f"{rec.get('SizeGB', 0)} GB",
                                    "savings": rec.get("EstimatedMonthlyCost", 0) * 0.8,  # Assume 80% savings with IA
                                }
                            )

        # Generate HTML for resource groups
        if resource_groups:
            content += '<div class="affected-resources">'
            content += "<h4>Affected Resources by Recommendation Type:</h4>"

            for group_name, resources in resource_groups.items():
                if resources:  # Only show groups with resources
                    total_savings = sum(r["savings"] for r in resources)
                    content += f'<div class="resource-group">'
                    content += f"<h5>{group_name} ({len(resources)} resources)</h5>"
                    if total_savings > 0:
                        content += f'<p class="group-savings">Potential Monthly Savings: ${total_savings:.2f}</p>'

                    content += '<ul class="resource-list">'
                    for resource in resources:
                        content += f"<li><strong>{resource['id']}</strong> ({resource['type']})"
                        if resource["savings"] > 0:
                            content += f" - ${resource['savings']:.2f}/month"
                        content += "</li>"

                    content += "</ul></div>"

            content += "</div>"

        return content

    def _calculate_service_savings(self, service_key: str, service_data: Dict[str, Any]) -> float:
        """Calculate realistic savings for services showing $0.00"""
        if service_data.get("total_monthly_savings", 0) > 0:
            return service_data["total_monthly_savings"]

        # Calculate savings based on recommendations when JSON shows $0
        total_savings = 0
        sources = service_data.get("sources", {})

        for source_name, source_data in sources.items():
            # Handle both old format (dict) and new format (list)
            if isinstance(source_data, dict):
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                recommendations = source_data
            else:
                recommendations = []

            for rec in recommendations:
                if service_key == "ec2":
                    # EC2 savings calculation
                    recommendation = rec.get("Recommendation", "").lower()
                    if "previous generation" in recommendation:
                        total_savings += 50
                    elif "dedicated tenancy" in recommendation:
                        total_savings += 200
                    elif "burstable" in recommendation:
                        total_savings += 30
                    elif "spot" in recommendation:
                        total_savings += 100
                    elif "schedule" in recommendation:
                        total_savings += 150
                    elif rec.get("estimatedMonthlySavings", 0) > 0:
                        total_savings += rec.get("estimatedMonthlySavings", 0)
                    else:
                        total_savings += 25  # Default EC2 optimization

                elif service_key == "dynamodb":
                    # DynamoDB savings calculation
                    recommendation = rec.get("Recommendation", "").lower()
                    if "on-demand" in recommendation:
                        total_savings += 100
                    elif "provisioned" in recommendation:
                        total_savings += 75
                    elif "reserved" in recommendation:
                        total_savings += 200
                    else:
                        total_savings += 50  # Default DynamoDB optimization

                elif service_key in ["opensearch", "api_gateway", "step_functions"]:
                    # Other services - add default savings per recommendation
                    total_savings += 50

        return total_savings

    def _get_service_content(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get content for a specific service tab"""
        # Calculate realistic savings if JSON shows $0
        calculated_savings = self._calculate_service_savings(service_key, service_data)

        # Update service data with calculated savings
        if calculated_savings > 0:
            service_data = service_data.copy()
            service_data["total_monthly_savings"] = calculated_savings

        # Savings Plans removed - will be generated from another source

        # Special handler for AMI - use grouped format
        if service_key == "ami":
            sources = service_data.get("sources", {})
            all_amis = []
            for source_data in sources.values():
                if isinstance(source_data, dict):
                    all_amis.extend(source_data.get("recommendations", []))
                elif isinstance(source_data, list):
                    all_amis.extend(source_data)
            return self._get_amis_content({"count": len(all_amis), "recommendations": all_amis})

        # Filter out non-relevant recommendations
        filtered_service_data = self._filter_recommendations(service_data)

        content = f'<div class="service-header">'
        content += f'<h2 class="service-title">{filtered_service_data["service_name"]} Cost Optimization</h2>'
        content += self._get_service_stats(service_key, filtered_service_data)
        content += "</div>"

        # Affected resources list (skip for services with full grouping to avoid duplication)
        if service_key not in [
            "ebs",
            "ec2",
            "rds",
            "s3",
            "dynamodb",
            "containers",
            "elasticache",
            "opensearch",
            "file_systems",
            "network",
            "monitoring",
            "additional_services",
            "lambda",
            "cloudfront",
            "api_gateway",
            "step_functions",
            "auto_scaling",
            "backup",
            "route53",
            "ami",
            "lightsail",
            "dms",
            "glue",
            "redshift",
        ]:
            content += self._get_affected_resources_list(service_key, filtered_service_data)

        # Recommendations section
        content += '<div class="recommendation-section">'
        content += '<h3 class="section-title">💡 Optimization Recommendations</h3>'
        content += f'<div class="rec-summary"><strong>Total Recommendations:</strong> {filtered_service_data["total_recommendations"]} | '
        content += f'<strong>Estimated Monthly Savings:</strong> <span class="savings">${filtered_service_data["total_monthly_savings"]:.2f}</span></div>'

        # Optimization opportunities
        if "optimization_descriptions" in filtered_service_data:
            content += '<div class="opportunities">'
            for desc_key, desc in list(filtered_service_data["optimization_descriptions"].items())[:5]:  # Show top 5
                content += f"""<div class="opportunity">
                    <h4>{desc.get("title", "")}</h4>
                    <p>{desc.get("description", "")}</p>
                </div>"""
            content += "</div>"

        # Detailed recommendations
        content += self._get_detailed_recommendations(service_key, filtered_service_data)
        content += "</div>"

        return content

    def _get_service_stats(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """Get service-specific statistics"""
        stats_html = '<div class="service-stats">'
        has_stats = False

        if service_key == "ec2":
            stats_html += f'<div class="stat-card"><h4>EC2 Instances</h4><div class="value">{service_data.get("instance_count", 0)}</div></div>'
            has_stats = True

        elif service_key == "ebs":
            counts = service_data.get("volume_counts", {})
            stats_html += (
                f'<div class="stat-card"><h4>Total Volumes</h4><div class="value">{counts.get("total", 0)}</div></div>'
            )
            stats_html += f'<div class="stat-card"><h4>Unattached</h4><div class="value">{counts.get("unattached", 0)}</div></div>'
            stats_html += (
                f'<div class="stat-card"><h4>gp2 Volumes</h4><div class="value">{counts.get("gp2", 0)}</div></div>'
            )
            has_stats = True

        elif service_key == "rds":
            counts = service_data.get("instance_counts", {})
            stats_html += f'<div class="stat-card"><h4>Total Instances</h4><div class="value">{counts.get("total", 0)}</div></div>'
            stats_html += (
                f'<div class="stat-card"><h4>Running</h4><div class="value">{counts.get("running", 0)}</div></div>'
            )
            stats_html += (
                f'<div class="stat-card"><h4>MySQL</h4><div class="value">{counts.get("mysql", 0)}</div></div>'
            )
            has_stats = True

        elif service_key == "file_systems":
            efs_counts = service_data.get("efs_counts", {})
            fsx_counts = service_data.get("fsx_counts", {})
            stats_html += f'<div class="stat-card"><h4>EFS Systems</h4><div class="value">{efs_counts.get("total", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>FSx Systems</h4><div class="value">{fsx_counts.get("total", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>EFS Size (GB)</h4><div class="value">{efs_counts.get("total_size_gb", 0)}</div></div>'
            has_stats = True

        elif service_key == "s3":
            counts = service_data.get("bucket_counts", {})
            stats_html += (
                f'<div class="stat-card"><h4>Total Buckets</h4><div class="value">{counts.get("total", 0)}</div></div>'
            )
            stats_html += f'<div class="stat-card"><h4>No Lifecycle</h4><div class="value">{counts.get("without_lifecycle", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>No Intelligent Tiering</h4><div class="value">{counts.get("without_intelligent_tiering", 0)}</div></div>'
            has_stats = True

            # Add top cost and size buckets
            sources = service_data.get("sources", {})
            s3_data = sources.get("s3_bucket_analysis", {})
            top_cost = s3_data.get("top_cost_buckets", [])
            top_size = s3_data.get("top_size_buckets", [])

            if top_cost:
                stats_html += f'<div class="stat-card"><h4>Highest Cost Bucket</h4><div class="value">${top_cost[0].get("EstimatedMonthlyCost", 0):.2f}/mo</div></div>'
            if top_size:
                stats_html += f'<div class="stat-card"><h4>Largest Bucket</h4><div class="value">{top_size[0].get("SizeGB", 0):.1f} GB</div></div>'

        elif service_key == "dynamodb":
            counts = service_data.get("table_counts", {})
            stats_html += (
                f'<div class="stat-card"><h4>Total Tables</h4><div class="value">{counts.get("total", 0)}</div></div>'
            )
            stats_html += f'<div class="stat-card"><h4>Provisioned</h4><div class="value">{counts.get("provisioned", 0)}</div></div>'
            stats_html += (
                f'<div class="stat-card"><h4>On-Demand</h4><div class="value">{counts.get("on_demand", 0)}</div></div>'
            )
            has_stats = True

        elif service_key == "containers":
            counts = service_data.get("service_counts", {})
            stats_html += f'<div class="stat-card"><h4>ECS Clusters</h4><div class="value">{counts.get("ecs_clusters", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>EKS Clusters</h4><div class="value">{counts.get("eks_clusters", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>ECR Repositories</h4><div class="value">{counts.get("ecr_repositories", 0)}</div></div>'
            stats_html += f'<div class="stat-card"><h4>ECS Services</h4><div class="value">{counts.get("ecs_services", 0)}</div></div>'
            has_stats = True

        stats_html += "</div>"

        # Return empty string if no stats to avoid empty container
        return stats_html if has_stats else ""

    def _get_detailed_recommendations(self, service_key: str, service_data: Dict[str, Any]) -> str:
        """
        Generate detailed recommendations HTML for a specific AWS service.

        This method is the core of the smart grouping system that organizes
        recommendations by category for better readability and actionability.

        Smart Grouping Strategy:
        - Groups similar recommendations together by category
        - Deduplicates findings across multiple data sources
        - Applies consistent formatting and styling
        - Calculates aggregated savings for grouped recommendations
        - Provides clear, actionable recommendations for each group

        Services with Full Grouping (11 services):
        - EC2, EBS, RDS, S3, DynamoDB, Containers
        - ElastiCache, OpenSearch, File Systems, Network, Monitoring

        Args:
            service_key (str): AWS service identifier (e.g., 'ec2', 'rds', 'elasticache')
            service_data (Dict[str, Any]): Service-specific scan results and recommendations

        Returns:
            str: HTML content with grouped recommendations and consistent styling

        Note:
            - Handles deduplication across multiple data sources
            - Applies service-specific grouping logic
            - Maintains consistent UI patterns across all services
            - Automatically hides empty groups
        """
        content = '<div class="recommendation-list">'

        sources = service_data.get("sources", {})

        # Handle services with full grouping BEFORE the source loop
        # This prevents duplicate processing and ensures proper deduplication
        if service_key == "file_systems":
            # File Systems: Group by EFS vs FSx and optimization type (lifecycle, tiering, deduplication)
            grouped_fs = {
                "EFS No Lifecycle": [],  # EFS without lifecycle policies (47-94% savings)
                "FSx Optimization": [],  # FSx optimization opportunities (30-80% savings)
            }

            # Collect all recommendations from all sources first to enable deduplication
            all_fs_recs = []
            for src_name, src_data in sources.items():
                if src_data.get("count", 0) > 0:
                    all_fs_recs.extend(src_data.get("recommendations", []))

            # Deduplicate file systems across sources (same FS may appear multiple times)
            seen_fs = {}
            for rec in all_fs_recs:
                fs_id = rec.get("FileSystemId", "Unknown")

                # Keep first occurrence with best name (prefer named over 'Unnamed')
                if fs_id not in seen_fs:
                    seen_fs[fs_id] = rec
                elif rec.get("Name") and rec.get("Name") != "Unnamed":
                    # Update if this has a better name
                    seen_fs[fs_id] = rec

            # Group deduplicated file systems (use FileSystemType to distinguish EFS vs FSx)
            for fs_id, rec in seen_fs.items():
                # Use FileSystemType field to distinguish EFS from FSx
                if "FileSystemType" in rec:  # FSx has FileSystemType field
                    grouped_fs["FSx Optimization"].append(rec)
                elif "HasIAPolicy" in rec or fs_id.startswith("fs-0"):  # EFS indicators
                    if not rec.get("HasIAPolicy", True):
                        grouped_fs["EFS No Lifecycle"].append(rec)
                    # Skip EFS with lifecycle (no cost savings)
                else:
                    # Fallback: assume EFS if starts with fs-0, otherwise FSx
                    if fs_id.startswith("fs-0"):
                        if not rec.get("HasIAPolicy", True):
                            grouped_fs["EFS No Lifecycle"].append(rec)
                    else:
                        grouped_fs["FSx Optimization"].append(rec)

            for group_name, filesystems in grouped_fs.items():
                if not filesystems:
                    continue

                content += f'<div class="rec-item">'
                label = "file system" if len(filesystems) == 1 else "file systems"
                content += f"<h5>{group_name} ({len(filesystems)} {label})</h5>"

                if group_name == "EFS No Lifecycle":
                    content += "<p><strong>Recommendation:</strong> Enable lifecycle policies to move infrequently accessed files to IA storage (Save 80%)</p>"
                elif group_name == "FSx Optimization":
                    content += "<p><strong>Recommendation:</strong> Review FSx configuration for optimization opportunities</p>"

                content += "<p><strong>File Systems:</strong></p><ul>"
                for fs in filesystems:
                    fs_id = fs.get("FileSystemId", "Unknown")
                    fs_name = fs.get("Name", "Unnamed")
                    size = fs.get("SizeGB", 0)
                    content += f"<li>{fs_id} - {fs_name} ({size:.2f} GB)</li>"
                content += "</ul></div>"

            content += "</div>"
            return content

        elif service_key == "lambda":
            # Lambda: Group by CheckCategory (optimization type)
            grouped_lambda = {}

            # Collect all Lambda recommendations from all sources
            all_lambda_recs = []
            for src_name, src_data in sources.items():
                if src_data.get("count", 0) > 0:
                    all_lambda_recs.extend(src_data.get("recommendations", []))

            # Group by CheckCategory
            for rec in all_lambda_recs:
                category = rec.get("CheckCategory", "Lambda Optimization")
                if category not in grouped_lambda:
                    grouped_lambda[category] = []
                grouped_lambda[category].append(rec)

            # Display grouped Lambda functions
            for category, functions in grouped_lambda.items():
                if not functions:
                    continue

                content += f'<div class="rec-item">'
                label = "function" if len(functions) == 1 else "functions"
                content += f"<h5>{category} ({len(functions)} {label})</h5>"

                # Show common recommendation for the category
                if functions:
                    content += f"<p><strong>Recommendation:</strong> {functions[0].get('Recommendation', 'Optimize Lambda function')}</p>"
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> {functions[0].get("EstimatedSavings", "Cost optimization")}</p>'

                content += "<p><strong>Functions:</strong></p><ul>"
                for func in functions:
                    # Handle both Cost Optimization Hub and enhanced checks formats
                    func_name = func.get("FunctionName") or func.get("resourceId", "Unknown")
                    memory = func.get("MemorySize", "")
                    timeout = func.get("Timeout", "")
                    runtime = func.get("Runtime", "")

                    # For Cost Optimization Hub recommendations, extract additional details
                    if "currentResourceDetails" in func:
                        lambda_config = (
                            func.get("currentResourceDetails", {}).get("lambdaFunction", {}).get("configuration", {})
                        )
                        compute_config = lambda_config.get("compute", {})
                        if not memory and "memorySizeInMB" in compute_config:
                            memory = compute_config["memorySizeInMB"]
                        if not runtime and "architecture" in compute_config:
                            runtime = compute_config["architecture"]

                    details = []
                    if memory:
                        details.append(f"{memory}MB")
                    if timeout:
                        details.append(f"{timeout}s timeout")
                    if runtime:
                        details.append(runtime)

                    detail_str = f" ({', '.join(details)})" if details else ""
                    content += f"<li>{func_name}{detail_str}</li>"
                content += "</ul></div>"

            content += "</div>"
            return content

        elif service_key == "cloudfront":
            # CloudFront: Group by CheckCategory (optimization type)
            grouped_cloudfront = {}

            # Collect all CloudFront recommendations from all sources
            all_cf_recs = []
            for src_name, src_data in sources.items():
                if src_data.get("count", 0) > 0:
                    all_cf_recs.extend(src_data.get("recommendations", []))

            # Group by CheckCategory
            for rec in all_cf_recs:
                category = rec.get("CheckCategory", "CloudFront Optimization")
                if category not in grouped_cloudfront:
                    grouped_cloudfront[category] = []
                grouped_cloudfront[category].append(rec)

            # Display grouped CloudFront distributions
            for category, distributions in grouped_cloudfront.items():
                if not distributions:
                    continue

                content += f'<div class="rec-item">'
                label = "distribution" if len(distributions) == 1 else "distributions"
                content += f"<h5>{category} ({len(distributions)} {label})</h5>"

                # Show common recommendation for the category
                if distributions:
                    content += f"<p><strong>Recommendation:</strong> {distributions[0].get('Recommendation', 'Optimize CloudFront distribution')}</p>"
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> {distributions[0].get("EstimatedSavings", "Cost optimization")}</p>'

                content += "<p><strong>Distributions:</strong></p><ul>"
                for dist in distributions:
                    dist_id = dist.get("DistributionId", "Unknown")
                    domain_name = dist.get("DomainName", "")
                    status = dist.get("Status", "")
                    price_class = dist.get("PriceClass", "")

                    details = []
                    if domain_name:
                        details.append(domain_name)
                    if status:
                        details.append(f"Status: {status}")
                    if price_class:
                        details.append(f"Price Class: {price_class}")

                    detail_str = f" ({', '.join(details)})" if details else ""
                    content += f"<li>{dist_id}{detail_str}</li>"
                content += "</ul></div>"

            content += "</div>"
            return content

        elif service_key == "rds":
            # RDS: Group by CheckCategory (optimization type)
            grouped_rds = {}

            all_rds_recs = []
            for src_name, src_data in sources.items():
                if src_data.get("count", 0) > 0:
                    all_rds_recs.extend(src_data.get("recommendations", []))

            for rec in all_rds_recs:
                category = rec.get("CheckCategory", "RDS Optimization")
                if category not in grouped_rds:
                    grouped_rds[category] = []
                grouped_rds[category].append(rec)

            for category, resources in grouped_rds.items():
                if not resources:
                    continue

                content += f'<div class="rec-item">'
                label = "resource" if len(resources) == 1 else "resources"
                content += f"<h5>{category} ({len(resources)} {label})</h5>"

                if resources:
                    # Show the most common recommendation or a summary
                    recommendations = [r.get("Recommendation", "") for r in resources if r.get("Recommendation")]
                    if recommendations:
                        # Use the first specific recommendation
                        content += f"<p><strong>Recommendation:</strong> {recommendations[0]}</p>"
                    else:
                        # Fallback with more specific guidance
                        content += f"<p><strong>Recommendation:</strong> Review RDS instances for rightsizing, Reserved Instance opportunities, and Graviton migration. Consider Aurora for better performance per dollar.</p>"

                    # Show savings if available
                    savings = resources[0].get("EstimatedSavings", "")
                    if savings and savings != "Cost optimization":
                        content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings}</p>'
                    else:
                        content += f'<p class="savings"><strong>Estimated Savings:</strong> 20-72% potential cost reduction through optimization</p>'

                content += "<p><strong>Resources:</strong></p><ul>"
                for res in resources:
                    # Extract resource ID from multiple possible fields
                    resource_id = (
                        res.get("DBInstanceIdentifier")
                        or res.get("DBClusterIdentifier")
                        or res.get("dbClusterIdentifier")  # Compute Optimizer format
                        or res.get("dbInstanceIdentifier")  # Compute Optimizer format
                        or res.get("SnapshotId")
                        or res.get("ResourceId")
                        or res.get("resourceArn", "").split(":")[-1]
                        if res.get("resourceArn")
                        else "Unknown"
                    )

                    instance_class = res.get("DBInstanceClass", "")
                    engine = res.get("Engine", res.get("engine", ""))

                    details = []
                    if instance_class:
                        details.append(instance_class)
                    if engine:
                        details.append(engine)

                    detail_str = f" ({', '.join(details)})" if details else ""
                    content += f"<li>{resource_id}{detail_str}</li>"
                content += "</ul></div>"

            content += "</div>"
            return content

        elif service_key in ["lightsail", "dms", "glue"]:
            # Grouping for Lightsail, DMS, Glue
            grouped_resources = {}

            all_recs = []
            for src_name, src_data in sources.items():
                # Handle both dict and list formats
                if isinstance(src_data, dict):
                    if src_data.get("count", 0) > 0:
                        all_recs.extend(src_data.get("recommendations", []))
                elif isinstance(src_data, list):
                    all_recs.extend(src_data)

            for rec in all_recs:
                category = rec.get("CheckCategory", f"{service_key.replace('_', ' ').title()} Optimization")
                if category not in grouped_resources:
                    grouped_resources[category] = []
                grouped_resources[category].append(rec)

            for category, resources in grouped_resources.items():
                if not resources:
                    continue

                content += f'<div class="rec-item">'
                label = "resource" if len(resources) == 1 else "resources"
                content += f"<h5>{category} ({len(resources)} {label})</h5>"

                # Show common recommendation for the category
                if resources:
                    content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"
                    if resources[0].get("EstimatedSavings"):
                        content += f'<p class="savings"><strong>Estimated Savings:</strong> {resources[0].get("EstimatedSavings", "Cost optimization")}</p>'

                content += "<p><strong>Resources:</strong></p><ul>"
                for res in resources:
                    # Extract resource-specific identifier and details
                    if service_key == "lightsail":
                        resource_id = res.get("StaticIpName", res.get("InstanceName", "Unknown"))
                        ip = res.get("IpAddress", "")
                        detail_str = f" ({ip})" if ip else ""
                    elif service_key == "dms":
                        resource_id = res.get("InstanceId", "Unknown")
                        instance_class = res.get("InstanceClass", "")
                        cpu = res.get("AvgCPU", "")
                        details = []
                        if instance_class:
                            details.append(instance_class)
                        if cpu:
                            details.append(f"{cpu} CPU")
                        detail_str = f" ({', '.join(details)})" if details else ""
                    elif service_key == "glue":
                        resource_id = res.get("JobName", "Unknown")
                        worker_type = res.get("WorkerType", "")
                        num_workers = res.get("NumberOfWorkers", "")
                        details = []
                        if worker_type:
                            details.append(worker_type)
                        if num_workers:
                            details.append(f"{num_workers} workers")
                        detail_str = f" ({', '.join(details)})" if details else ""
                    else:
                        resource_id = "Unknown"
                        detail_str = ""

                    content += f"<li>{resource_id}{detail_str}</li>"
                content += "</ul></div>"

                content += "</div>"

            content += "</div>"
            return content

        elif service_key in ["api_gateway", "step_functions", "auto_scaling", "backup", "route53"]:
            # Generic grouping for remaining services
            grouped_resources = {}

            all_recs = []
            for src_name, src_data in sources.items():
                if src_data.get("count", 0) > 0:
                    all_recs.extend(src_data.get("recommendations", []))

            for rec in all_recs:
                category = rec.get("CheckCategory", f"{service_key.replace('_', ' ').title()} Optimization")
                if category not in grouped_resources:
                    grouped_resources[category] = []
                grouped_resources[category].append(rec)

            for category, resources in grouped_resources.items():
                if not resources:
                    continue

                content += f'<div class="rec-item">'
                label = "resource" if len(resources) == 1 else "resources"
                content += f"<h5>{category} ({len(resources)} {label})</h5>"

                if resources:
                    content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> {resources[0].get("EstimatedSavings", "Cost optimization")}</p>'

                content += "<p><strong>Resources:</strong></p><ul>"
                for res in resources:
                    # Extract appropriate resource identifier based on service
                    if service_key == "api_gateway":
                        resource_id = res.get("ApiId", res.get("RestApiId", res.get("ApiName", "Unknown")))
                    elif service_key == "step_functions":
                        resource_id = (
                            res.get("StateMachineArn", "Unknown").split(":")[-1]
                            if res.get("StateMachineArn")
                            else res.get("StateMachineName", "Unknown")
                        )
                    elif service_key == "auto_scaling":
                        resource_id = res.get("AutoScalingGroupName", res.get("GroupName", "Unknown"))
                    elif service_key == "backup":
                        resource_id = res.get(
                            "BackupPlanName", res.get("BackupVaultName", res.get("PlanName", "Unknown"))
                        )
                    elif service_key == "route53":
                        resource_id = res.get("HostedZoneId", res.get("HealthCheckId", res.get("ZoneId", "Unknown")))
                    elif service_key == "monitoring":
                        resource_id = res.get(
                            "AlarmName",
                            res.get(
                                "LogGroupName",
                                res.get(
                                    "TrailName",
                                    res.get("Namespace", res.get("HostedZoneId", res.get("HealthCheckId", "Unknown"))),
                                ),
                            ),
                        )
                    elif service_key == "lightsail":
                        resource_id = res.get("InstanceName", res.get("StaticIpName", res.get("Name", "Unknown")))
                    elif service_key == "dms":
                        resource_id = res.get("InstanceId", res.get("ReplicationInstanceIdentifier", "Unknown"))
                    elif service_key == "glue":
                        resource_id = res.get("JobName", res.get("Name", "Unknown"))
                    else:
                        resource_id = "Unknown"

                    content += f"<li>{resource_id}</li>"
                content += "</ul></div>"

            content += "</div>"
            return content

        for source_name, source_data in sources.items():
            # Handle both old format (dict) and new format (list)
            if isinstance(source_data, dict):
                count = source_data.get("count", 0)
                recommendations = source_data.get("recommendations", [])
            elif isinstance(source_data, list):
                count = len(source_data)
                recommendations = source_data
            else:
                count = 0
                recommendations = []

            if count > 0:
                # Filter out invalid recommendations for EC2
                if service_key == "ec2":
                    filtered_recs = []
                    for rec in recommendations:
                        # Skip EBS volumes
                        if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
                            continue
                        # Skip Reserved Instances (they're for RDS/ElastiCache)
                        if rec.get("actionType") == "PurchaseReservedInstances":
                            continue
                        # Skip N/A resources
                        if rec.get("actionType") and rec.get("resourceId") == "N/A":
                            continue
                        filtered_recs.append(rec)
                    recommendations = filtered_recs

                total_count = len(recommendations)
                if total_count == 0:
                    continue

                # Skip section headers for services with full grouping
                if service_key not in [
                    "ebs",
                    "ec2",
                    "s3",
                    "dynamodb",
                    "containers",
                    "elasticache",
                    "opensearch",
                    "file_systems",
                    "network",
                    "monitoring",
                    "additional_services",
                    "rds",
                    "lightsail",
                    "dms",
                    "glue",
                    "redshift",
                ]:
                    content += f"<h4>{source_name.replace('_', ' ').title()}: {total_count} items</h4>"

                # Group recommendations by CheckCategory for EC2 enhanced checks
                if service_key == "ec2" and source_name == "enhanced_checks":
                    grouped_recs = {}
                    for rec in recommendations:
                        # Skip ECS resources in EC2 section - comprehensive check
                        resource_id = rec.get(
                            "InstanceId",
                            rec.get(
                                "ImageId",
                                rec.get("AllocationId", rec.get("ResourceId", rec.get("resourceId", "Resource"))),
                            ),
                        )
                        resource_name = rec.get("Name", rec.get("ResourceName", ""))

                        # Check all possible fields for ECS patterns
                        all_text = f"{resource_id} {resource_name}".lower()
                        if (
                            "ecs-cluster" in all_text
                            or "/cronjob" in all_text
                            or "forwarder" in all_text
                            or "lambda" in all_text
                            or "ecs" in all_text
                        ):
                            continue

                        # Skip spot and optimized
                        if "CheckCategory" in rec and "Spot" in rec.get("CheckCategory", ""):
                            continue
                        if "Recommendation" in rec and "spot instance" in rec.get("Recommendation", "").lower():
                            continue
                        finding = rec.get("finding", rec.get("instanceFinding", rec.get("InstanceFinding", ""))).lower()
                        if finding == "optimized":
                            continue

                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_recs:
                            grouped_recs[category] = []
                        grouped_recs[category].append(rec)

                    # Display grouped recommendations
                    for category, recs in grouped_recs.items():
                        content += f'<div class="rec-item">'
                        content += f"<h5>{category} ({len(recs)} resources)</h5>"
                        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize resource')}</p>"
                        content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'
                        content += "<p><strong>Affected Resources:</strong></p><ul>"
                        for rec in recs:
                            resource_id = rec.get("InstanceId", rec.get("ImageId", rec.get("AllocationId", "Resource")))
                            instance_type = rec.get("InstanceType", "")
                            if instance_type:
                                content += f"<li>{resource_id} ({instance_type})</li>"
                            else:
                                content += f"<li>{resource_id}</li>"
                        content += "</ul></div>"
                    continue

                # Group Cost Optimization Hub recommendations by actionType
                if service_key == "ec2" and source_name == "cost_optimization_hub":
                    grouped_actions = {}
                    for rec in recommendations:
                        if "actionType" not in rec:
                            continue
                        # Skip ECS resources in EC2 section
                        resource_details = rec.get("currentResourceDetails", {})
                        if "ecsService" in resource_details or "ecsCluster" in resource_details:
                            continue
                        # Additional ECS filtering by resource ID/name patterns
                        resource_id = rec.get("resourceId", "N/A")
                        resource_name = rec.get("Name", rec.get("ResourceName", ""))
                        all_text = f"{resource_id} {resource_name}".lower()
                        if (
                            "ecs-cluster" in all_text
                            or "/cronjob" in all_text
                            or "forwarder" in all_text
                            or "lambda" in all_text
                            or "ecs" in all_text
                        ):
                            continue
                        # Skip spot and optimized
                        if "Recommendation" in rec and "spot instance" in rec.get("Recommendation", "").lower():
                            continue
                        finding = rec.get("finding", "").lower()
                        if finding == "optimized":
                            continue

                        action = rec.get("actionType", "Other")
                        if action not in grouped_actions:
                            grouped_actions[action] = []
                        grouped_actions[action].append(rec)

                    # Display grouped actions
                    for action, recs in grouped_actions.items():
                        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
                        content += f'<div class="rec-item">'
                        content += f"<h5>Action: {action} ({len(recs)} resources)</h5>"
                        content += (
                            f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_savings:.2f}</p>'
                        )
                        content += "<p><strong>Resources:</strong></p><ul>"
                        for rec in recs:
                            resource_id = rec.get("resourceId", "N/A")
                            current_type = (
                                rec.get("currentResourceDetails", {})
                                .get("ec2Instance", {})
                                .get("configuration", {})
                                .get("instance", {})
                                .get("type", "N/A")
                            )
                            rec_type = (
                                rec.get("recommendedResourceDetails", {})
                                .get("ec2Instance", {})
                                .get("configuration", {})
                                .get("instance", {})
                                .get("type", "N/A")
                            )
                            savings = rec.get("estimatedMonthlySavings", 0)

                            if current_type != "N/A" and rec_type != "N/A":
                                content += f"<li>{resource_id}: {current_type} → {rec_type} (${savings:.2f}/month)</li>"
                            else:
                                content += f"<li>{resource_id} (${savings:.2f}/month)</li>"
                        content += "</ul></div>"
                    continue

                # Group Cost Optimization Hub recommendations by actionType for EBS (gp2→gp3 migration)
                if service_key == "ebs" and source_name == "cost_optimization_hub":
                    grouped_actions = {}
                    for rec in recommendations:
                        if "actionType" not in rec or "ebsVolume" not in rec.get("currentResourceDetails", {}):
                            continue
                        finding = rec.get("finding", "").lower()
                        if finding == "optimized":
                            continue

                        action = rec.get("actionType", "Other")
                        if action not in grouped_actions:
                            grouped_actions[action] = []
                        grouped_actions[action].append(rec)

                    # Display grouped actions
                    for action, recs in grouped_actions.items():
                        total_savings = sum(r.get("estimatedMonthlySavings", 0) for r in recs)
                        content += f'<div class="rec-item">'
                        content += f"<h5>Action: {action} ({len(recs)} volumes)</h5>"
                        content += (
                            f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_savings:.2f}</p>'
                        )
                        content += "<p><strong>Volumes:</strong></p><ul>"
                        for rec in recs:
                            resource_id = rec.get("resourceId", "N/A")
                            ebs_config = (
                                rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
                            )
                            volume_type = ebs_config.get("storage", {}).get("type", "N/A")
                            volume_size = ebs_config.get("storage", {}).get("sizeInGb", 0)
                            savings = rec.get("estimatedMonthlySavings", 0)

                            content += (
                                f"<li>{resource_id}: {volume_type} ({volume_size} GB) - ${savings:.2f}/month</li>"
                            )
                        content += "</ul></div>"
                    continue

                # Group unattached volumes
                if service_key == "ebs" and source_name == "unattached_volumes":
                    total_cost = sum(r.get("EstimatedMonthlyCost", 0) for r in recommendations)
                    content += f'<div class="rec-item">'
                    content += f"<h5>Unattached Volumes ({len(recommendations)} volumes)</h5>"
                    content += f"<p><strong>Recommendation:</strong> Delete unattached volumes (create snapshots first if needed)</p>"
                    content += f'<p class="savings"><strong>Total Monthly Savings:</strong> ${total_cost:.2f}</p>'
                    content += "<p><strong>Volumes:</strong></p><ul>"

                    for rec in recommendations:
                        volume_id = rec.get("VolumeId", "N/A")
                        volume_type = rec.get("VolumeType", "N/A")
                        size = rec.get("Size", 0)
                        cost = rec.get("EstimatedMonthlyCost", 0)
                        content += f"<li>{volume_id}: {volume_type} ({size} GB) - ${cost:.2f}/month</li>"

                    content += "</ul></div>"
                    continue

                # Group gp2 migration recommendations
                if service_key == "ebs" and source_name == "gp2_migration":
                    content += f'<div class="rec-item">'
                    content += f"<h5>gp2 to gp3 Migration ({len(recommendations)} volumes)</h5>"
                    content += (
                        f"<p><strong>Recommendation:</strong> Migrate gp2 volumes to gp3 for 20% cost savings</p>"
                    )
                    content += f'<p class="savings"><strong>Estimated Savings:</strong> 20% cost reduction</p>'
                    content += "<p><strong>Volumes:</strong></p><ul>"

                    for rec in recommendations:
                        volume_id = rec.get("VolumeId", "N/A")
                        size = rec.get("Size", 0)
                        content += f"<li>{volume_id}: {size} GB</li>"

                    content += "</ul></div>"
                    continue

                # Group EBS enhanced checks by CheckCategory
                if service_key == "ebs" and source_name == "enhanced_checks":
                    grouped_checks = {}
                    for rec in recommendations:
                        if "CheckCategory" not in rec:
                            continue

                        category = rec.get("CheckCategory", "Other")

                        # Skip snapshot-related checks (they're in Snapshots tab)
                        if "snapshot" in category.lower():
                            continue

                        # Skip unused encrypted volumes (duplicate of unattached volumes)
                        if "unused encrypted" in category.lower():
                            continue

                        # Skip unattached volumes (already shown from dedicated source)
                        if "unattached" in category.lower():
                            continue

                        if category not in grouped_checks:
                            grouped_checks[category] = []
                        grouped_checks[category].append(rec)

                    # Display grouped checks
                    for category, recs in grouped_checks.items():
                        total_savings = 0
                        for r in recs:
                            savings_str = r.get("EstimatedSavings", "")
                            if "$" in savings_str:
                                try:
                                    total_savings += float(savings_str.replace("$", "").split("/")[0])
                                except (ValueError, AttributeError) as e:
                                    print(f"⚠️ Could not parse EBS savings '{savings_str}': {str(e)}")
                                    # Continue without this savings amount

                        content += f'<div class="rec-item">'
                        content += f"<h5>{category} ({len(recs)} volumes)</h5>"
                        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize volumes')}</p>"
                        if total_savings > 0:
                            content += (
                                f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
                            )
                        else:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'
                        content += "<p><strong>Volumes:</strong></p><ul>"

                        for rec in recs:
                            volume_id = rec.get("VolumeId", rec.get("SnapshotId", "N/A"))
                            if "Size" in rec:
                                content += f"<li>{volume_id}: {rec.get('Size')} GB"
                                if "CurrentType" in rec:
                                    content += f" ({rec.get('CurrentType')} → {rec.get('RecommendedType')})"
                                content += "</li>"
                            else:
                                content += f"<li>{volume_id}</li>"

                        content += "</ul></div>"
                    continue

                # Group Compute Optimizer recommendations by finding for EC2 (rightsizing, idle instances)
                if service_key == "ec2" and source_name == "compute_optimizer":
                    grouped_findings = {}
                    for rec in recommendations:
                        if "instanceArn" not in rec:
                            continue
                        finding = rec.get("finding", "Unknown")
                        # Skip optimized and under-provisioned (performance recommendations, not cost savings)
                        if finding.lower() == "optimized" or finding.upper() == "UNDER_PROVISIONED":
                            continue

                        if finding not in grouped_findings:
                            grouped_findings[finding] = []
                        grouped_findings[finding].append(rec)

                    # Display grouped findings
                    for finding, recs in grouped_findings.items():
                        content += f'<div class="rec-item">'
                        content += f"<h5>Finding: {finding} ({len(recs)} instances)</h5>"
                        content += "<p><strong>Instances:</strong></p><ul>"
                        for rec in recs:
                            instance_name = rec.get("instanceName", "N/A")
                            instance_id = rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
                            current_type = rec.get("currentInstanceType", "N/A")

                            # Get recommended instance type
                            rec_type = "N/A"
                            if rec.get("recommendationOptions"):
                                rec_type = rec["recommendationOptions"][0].get("instanceType", "N/A")

                            display_name = instance_name if instance_name != "N/A" else instance_id
                            if rec_type != "N/A":
                                content += f"<li>{display_name}: {current_type} → {rec_type}</li>"
                            else:
                                content += f"<li>{display_name}: {current_type}</li>"
                        content += "</ul></div>"
                    continue

                # Group Compute Optimizer recommendations by finding for EBS (gp2→gp3, over-provisioned)
                if service_key == "ebs" and source_name == "compute_optimizer":
                    grouped_findings = {}
                    for rec in recommendations:
                        if "volumeArn" not in rec:
                            continue
                        finding = rec.get("finding", "Unknown")
                        # Skip optimized and under-provisioned (performance recommendations, not cost savings)
                        if finding.lower() == "optimized" or finding.upper() == "UNDER_PROVISIONED":
                            continue

                        if finding not in grouped_findings:
                            grouped_findings[finding] = []
                        grouped_findings[finding].append(rec)

                    # Display grouped findings
                    for finding, recs in grouped_findings.items():
                        content += f'<div class="rec-item">'
                        content += f"<h5>Finding: {finding} ({len(recs)} volumes)</h5>"
                        content += "<p><strong>Volumes:</strong></p><ul>"
                        for rec in recs:
                            volume_id = rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
                            current_config = rec.get("currentConfiguration", {})
                            volume_type = current_config.get("volumeType", "N/A")
                            volume_size_data = current_config.get("volumeSize", 0)
                            volume_size = (
                                volume_size_data.get("value", 0)
                                if isinstance(volume_size_data, dict)
                                else volume_size_data
                            )

                            content += f"<li>{volume_id}: {volume_type} ({volume_size} GB)</li>"
                        content += "</ul></div>"
                    continue

                # Group Compute Optimizer recommendations by finding for RDS
                if service_key == "rds" and source_name == "compute_optimizer":
                    grouped_findings = {}
                    for rec in recommendations:
                        if "resourceArn" not in rec:
                            continue
                        finding = rec.get("instanceFinding", rec.get("finding", "Unknown"))
                        reason_codes = rec.get("instanceFindingReasonCodes", [])

                        # Skip optimized instances unless they have cost savings
                        if finding.lower() == "optimized":
                            # Check if there are actual savings
                            has_savings = False
                            if rec.get("instanceRecommendationOptions"):
                                for option in rec["instanceRecommendationOptions"]:
                                    savings = (
                                        option.get("savingsOpportunity", {})
                                        .get("estimatedMonthlySavings", {})
                                        .get("value", 0)
                                    )
                                    if savings > 0:
                                        has_savings = True
                                        break
                            if not has_savings:
                                continue

                        # Skip if no actionable recommendations
                        if finding == "Unknown" and not rec.get("instanceRecommendationOptions"):
                            continue

                        if finding not in grouped_findings:
                            grouped_findings[finding] = []
                        grouped_findings[finding].append(rec)

                    # Display grouped findings
                    for finding, recs in grouped_findings.items():
                        content += f'<div class="rec-item">'
                        count = len(recs)
                        label = "database" if count == 1 else "databases"
                        content += f"<h5>Finding: {finding} ({count} {label})</h5>"

                        # Show recommendation based on finding
                        if recs[0].get("instanceRecommendationOptions"):
                            content += "<p><strong>Recommendation:</strong> Optimize instance class for better performance or cost savings</p>"

                        content += "<p><strong>Databases:</strong></p><ul>"
                        for rec in recs:
                            resource_arn = rec.get("resourceArn", "N/A")
                            db_name = resource_arn.split(":")[-1] if resource_arn != "N/A" else "N/A"
                            engine = rec.get("engine", "Unknown")
                            engine_version = rec.get("engineVersion", "")
                            current_class = rec.get("currentDBInstanceClass", "N/A")

                            # Get recommended instance class
                            rec_class = "N/A"
                            if rec.get("instanceRecommendationOptions"):
                                rec_class = rec["instanceRecommendationOptions"][0].get("dbInstanceClass", "N/A")

                            # Build display string
                            display_str = f"{db_name} ({engine}"
                            if engine_version:
                                display_str += f" {engine_version}"
                            display_str += ")"

                            if current_class != "N/A":
                                display_str += f": {current_class}"
                                if rec_class != "N/A":
                                    display_str += f" → {rec_class}"

                            content += f"<li>{display_str}</li>"
                        content += "</ul></div>"
                    continue

                # Group RDS enhanced checks by category (Graviton migration, Reserved Instances, old snapshots)
                if service_key == "rds" and source_name == "enhanced_checks":
                    grouped_categories = {}
                    for rec in recommendations:
                        # Group by optimization type for better organization
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_categories:
                            grouped_categories[category] = []
                        grouped_categories[category].append(rec)

                    # Display grouped categories
                    for category, recs in grouped_categories.items():
                        content += f'<div class="rec-item">'

                        # Adjust label based on category
                        count = len(recs)
                        if "Snapshot" in category:
                            label = "snapshot" if count == 1 else "snapshots"
                            content += f"<h5>{category} ({count} {label})</h5>"
                        else:
                            label = "database" if count == 1 else "databases"
                            content += f"<h5>{category} ({count} {label})</h5>"

                        content += f"<p><strong>Recommendation:</strong> {recs[0].get('Recommendation', 'Optimize configuration')}</p>"

                        # Show estimated savings if available
                        total_savings = 0
                        has_numeric_savings = False
                        for rec in recs:
                            savings_str = rec.get("EstimatedSavings", "")
                            if "$" in savings_str and "/month" in savings_str:
                                try:
                                    # Remove currency, time unit, and any additional text like "(max estimate)"
                                    clean_str = savings_str.replace("$", "").replace("/month", "").split("(")[0].strip()
                                    savings_val = float(clean_str)
                                    total_savings += savings_val
                                    has_numeric_savings = True
                                except (ValueError, AttributeError) as e:
                                    print(f"⚠️ Could not parse grouped savings '{savings_str}': {str(e)}")
                                    # Continue without this savings amount

                        if has_numeric_savings:
                            content += (
                                f'<p class="savings"><strong>Estimated Savings:</strong> ${total_savings:.2f}/month</p>'
                            )
                        else:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {recs[0].get("EstimatedSavings", "Cost optimization")}</p>'

                        # Adjust label based on category
                        if "Snapshot" in category:
                            content += "<p><strong>Affected Snapshots:</strong></p><ul>"
                        else:
                            content += "<p><strong>Affected Databases:</strong></p><ul>"

                        for rec in recs:
                            db_id = rec.get("DBInstanceIdentifier", rec.get("SnapshotId", "Unknown"))
                            engine = rec.get("engine", "")
                            engine_version = rec.get("engineVersion", "")
                            finding = rec.get("instanceFinding", rec.get("storageFinding", ""))

                            # For snapshots, just show ID and finding (no engine info)
                            if "Snapshot" in category:
                                display_str = db_id
                                if finding:
                                    display_str += f" - {finding}"
                            else:
                                # For databases, show engine info
                                display_str = db_id
                                if engine:
                                    display_str += f" ({engine}"
                                    if engine_version:
                                        display_str += f" {engine_version}"
                                    display_str += ")"
                                if finding:
                                    display_str += f" - {finding}"

                            content += f"<li>{display_str}</li>"
                        content += "</ul></div>"
                    continue

                # Group S3 buckets by optimization opportunities (lifecycle policies, intelligent tiering)
                if service_key == "s3":
                    grouped_s3 = {
                        "No Lifecycle Policy": [],  # Buckets missing lifecycle policies (40-95% savings)
                        "No Intelligent Tiering": [],  # Buckets missing intelligent tiering (automatic cost optimization)
                        "Static Website Optimization": [],  # Static website specific optimizations
                        "Both Missing": [],  # Buckets missing both optimizations (highest savings potential)
                        "Other Optimizations": [],  # Other S3 cost optimization opportunities
                    }

                    for rec in recommendations:
                        # Handle both standard and enhanced S3 checks
                        bucket_name = rec.get("Name") or rec.get("BucketName", "Unknown")
                        bucket_size = rec.get("SizeGB", 0)
                        bucket_cost = rec.get("EstimatedMonthlyCost", 0)

                        # Skip recommendations without valid bucket names or with empty data
                        if bucket_name == "Unknown" or not bucket_name:
                            continue

                        # Skip enhanced checks without size data to avoid duplicates with standard analysis
                        if bucket_size == 0 and bucket_cost == 0 and rec.get("CheckCategory"):
                            continue

                        if bucket_name == "Unknown" and bucket_size == 0 and bucket_cost == 0:
                            continue

                        # Skip small buckets (below 10GB - savings not noticeable) but allow enhanced checks without size
                        if bucket_size < 10 and rec.get("SizeGB") is not None:
                            continue

                        has_lifecycle = rec.get("HasLifecyclePolicy", False)
                        has_tiering = rec.get("HasIntelligentTiering", False)
                        is_static_website = rec.get("IsStaticWebsite", False)

                        if is_static_website:
                            grouped_s3["Static Website Optimization"].append(rec)
                        elif not has_lifecycle and not has_tiering:
                            grouped_s3["Both Missing"].append(rec)
                        elif not has_lifecycle:
                            grouped_s3["No Lifecycle Policy"].append(rec)
                        elif not has_tiering:
                            grouped_s3["No Intelligent Tiering"].append(rec)
                        else:
                            grouped_s3["Other Optimizations"].append(rec)

                    # Display grouped S3 buckets
                    for group_name, buckets in grouped_s3.items():
                        if not buckets:
                            continue

                        total_size = sum(b.get("SizeGB", 0) for b in buckets)
                        total_cost = sum(b.get("EstimatedMonthlyCost", 0) for b in buckets)

                        content += f'<div class="rec-item">'
                        content += f"<h5>{group_name} ({len(buckets)} buckets, {total_size:.2f} GB total)</h5>"

                        if group_name == "No Lifecycle Policy":
                            content += "<p><strong>Recommendation:</strong> Implement lifecycle policies to automatically transition objects to cheaper storage classes</p>"
                            content += '<p class="savings"><strong>Potential Savings:</strong> 40-95% depending on access patterns</p>'
                        elif group_name == "No Intelligent Tiering":
                            content += "<p><strong>Recommendation:</strong> Enable Intelligent Tiering for automatic cost optimization</p>"
                            content += '<p class="savings"><strong>Potential Savings:</strong> Up to 95% for infrequently accessed data</p>'
                        elif group_name == "Static Website Optimization":
                            content += "<p><strong>Recommendation:</strong> Enable CloudFront CDN for reduced data transfer costs and improved performance</p>"
                            content += '<p class="savings"><strong>Potential Savings:</strong> 20-60% on data transfer costs</p>'
                        elif group_name == "Both Missing":
                            content += "<p><strong>Recommendation:</strong> Implement lifecycle policies AND enable Intelligent Tiering</p>"
                            content += '<p class="savings"><strong>Potential Savings:</strong> 40-95% depending on access patterns</p>'
                        else:
                            content += "<p><strong>Recommendation:</strong> Review other optimization opportunities</p>"

                        if total_cost > 0:
                            content += f"<p><strong>Current Monthly Cost:</strong> ${total_cost:.2f}</p>"

                        content += "<p><strong>Buckets:</strong></p><ul>"
                        for bucket in buckets:
                            bucket_name = bucket.get("Name") or bucket.get("BucketName", "Unknown")
                            bucket_size = bucket.get("SizeGB", 0)
                            bucket_cost = bucket.get("EstimatedMonthlyCost", 0)
                            content += f"<li>{bucket_name}: {bucket_size:.2f} GB"
                            if bucket_cost > 0:
                                content += f" (${bucket_cost:.2f}/month)"
                            content += "</li>"
                        content += "</ul></div>"
                    continue

                # Group DynamoDB tables by optimization opportunity (billing mode, auto scaling, reserved capacity)
                if service_key == "dynamodb":
                    grouped_dynamo = {
                        "Provisioned to On-Demand": [],  # Switch to On-Demand for unpredictable workloads
                        "On-Demand to Provisioned": [],  # Switch to Provisioned for predictable workloads (20-60% savings)
                        "Enable Auto Scaling": [],  # Enable auto scaling to optimize capacity
                        "Reserved Capacity": [],  # Purchase reserved capacity for steady workloads (53-76% savings)
                        "Other Optimizations": [],  # Other DynamoDB cost optimization opportunities
                    }

                    for rec in recommendations:
                        billing_mode = rec.get("BillingMode", "Unknown")
                        opportunities = rec.get("OptimizationOpportunities", [])
                        check_category = rec.get("CheckCategory", "")

                        # Handle both standard and enhanced DynamoDB checks
                        if "Switch to On-Demand" in str(opportunities) or "On-Demand" in check_category:
                            grouped_dynamo["Provisioned to On-Demand"].append(rec)
                        elif "Switch to Provisioned" in str(opportunities) or "Provisioned" in check_category:
                            grouped_dynamo["On-Demand to Provisioned"].append(rec)
                        elif "Enable Auto Scaling" in str(opportunities) or "Auto Scaling" in check_category:
                            grouped_dynamo["Enable Auto Scaling"].append(rec)
                        elif "Reserved Capacity" in str(opportunities) or "Reserved" in check_category:
                            grouped_dynamo["Reserved Capacity"].append(rec)
                        elif opportunities:
                            grouped_dynamo["Other Optimizations"].append(rec)
                        else:
                            continue

                    for group_name, tables in grouped_dynamo.items():
                        if not tables:
                            continue

                        content += f'<div class="rec-item">'
                        content += f"<h5>{group_name} ({len(tables)} tables)</h5>"

                        if group_name == "Provisioned to On-Demand":
                            content += "<p><strong>Recommendation:</strong> Switch to On-Demand billing for unpredictable workloads</p>"
                        elif group_name == "On-Demand to Provisioned":
                            content += "<p><strong>Recommendation:</strong> Switch to Provisioned mode for predictable workloads (Save 20-60%)</p>"
                        elif group_name == "Enable Auto Scaling":
                            content += (
                                "<p><strong>Recommendation:</strong> Enable Auto Scaling to optimize capacity</p>"
                            )
                        elif group_name == "Reserved Capacity":
                            content += "<p><strong>Recommendation:</strong> Purchase Reserved Capacity for steady workloads (Save 53-76%)</p>"

                        content += "<p><strong>Tables:</strong></p><ul>"
                        for table in tables:
                            table_name = table.get("TableName", "Unknown")
                            billing = table.get("BillingMode", "Unknown")
                            content += f"<li>{table_name} ({billing})</li>"
                        content += "</ul></div>"
                    continue

                # Group Container services by type and optimization (ECS, EKS, ECR lifecycle)
                if service_key == "containers":
                    grouped_containers = {
                        "ECS Container Insights Required": [],  # Enable Container Insights for metrics
                        "ECS Rightsizing - Metric-Backed": [],  # Downsize based on actual metrics
                        "ECS Over-Provisioned Services": [],  # Reduce desired count
                        "Unused ECS Clusters": [],  # ECS clusters with no running tasks (100% savings)
                        "Unused EKS Clusters": [],  # EKS clusters with no node groups (100% savings)
                        "ECR Lifecycle Missing": [],  # ECR repositories without lifecycle policies (storage costs)
                        "Other Optimizations": [],  # Other container optimization opportunities (30-90% savings)
                    }

                    for rec in recommendations:
                        check_category = rec.get("CheckCategory", "")

                        if check_category in grouped_containers:
                            grouped_containers[check_category].append(rec)
                        elif "ClusterName" in rec:
                            if "Version" in rec:  # EKS
                                if rec.get("Status") == "INACTIVE" or rec.get("NodeGroupsCount", 0) == 0:
                                    grouped_containers["Unused EKS Clusters"].append(rec)
                                else:
                                    grouped_containers["Other Optimizations"].append(rec)
                            else:  # ECS - fallback for items without CheckCategory
                                if rec.get("CheckCategory") == "Unused ECS Clusters":
                                    grouped_containers["Unused ECS Clusters"].append(rec)
                                else:
                                    grouped_containers["Other Optimizations"].append(rec)
                        elif "RepositoryName" in rec:
                            grouped_containers["ECR Lifecycle Missing"].append(rec)
                        else:
                            grouped_containers["Other Optimizations"].append(rec)

                    for group_name, resources in grouped_containers.items():
                        if not resources:
                            continue

                        content += f'<div class="rec-item">'
                        content += f"<h5>{group_name} ({len(resources)} resources)</h5>"

                        if group_name == "ECS Container Insights Required":
                            content += "<p><strong>Recommendation:</strong> Enable Container Insights to get metric-backed rightsizing recommendations</p>"
                        elif group_name == "ECS Rightsizing - Metric-Backed":
                            content += "<p><strong>Recommendation:</strong> Downsize task definitions based on measured low utilization over 7 days</p>"
                        elif group_name == "ECS Over-Provisioned Services":
                            content += "<p><strong>Recommendation:</strong> Reduce desired task count to match actual running tasks</p>"
                        elif group_name == "Unused ECS Clusters":
                            content += "<p><strong>Recommendation:</strong> Delete unused ECS clusters with no running tasks</p>"
                        elif group_name == "Unused EKS Clusters":
                            content += (
                                "<p><strong>Recommendation:</strong> Delete unused EKS clusters with no node groups</p>"
                            )
                        elif group_name == "ECR Lifecycle Missing":
                            content += "<p><strong>Recommendation:</strong> Implement lifecycle policies to automatically clean up old images and reduce storage costs</p>"
                        elif group_name == "Other Optimizations":
                            content += "<p><strong>Recommendation:</strong> Optimize container resources through rightsizing, Spot instances, and efficient scheduling</p>"

                        content += "<p><strong>Resources:</strong></p><ul>"
                        cluster_names = set()  # Deduplicate cluster names
                        for res in resources:
                            if "ClusterName" in res:
                                # Use ServiceName if available, otherwise ClusterName
                                if "ServiceName" in res:
                                    content += f"<li>{res.get('ServiceName', 'Unknown')} (Cluster: {res.get('ClusterName', 'Unknown')})</li>"
                                else:
                                    cluster_name = res.get("ClusterName", "Unknown")
                                    if cluster_name not in cluster_names:
                                        content += f"<li>{cluster_name}</li>"
                                        cluster_names.add(cluster_name)
                            elif "RepositoryName" in res:
                                content += f"<li>{res.get('RepositoryName', 'Unknown')} ({res.get('ImageCount', 0)} images)</li>"
                        content += "</ul></div>"
                    continue

                # Group ElastiCache by CheckCategory (Valkey migration, Graviton, Reserved Nodes, etc.)
                if service_key == "elasticache":
                    grouped_elasticache = {}
                    for rec in recommendations:
                        # Group by optimization type for better organization
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_elasticache:
                            grouped_elasticache[category] = []
                        grouped_elasticache[category].append(rec)

                    for category, clusters in grouped_elasticache.items():
                        if not clusters:
                            continue

                        content += f'<div class="rec-item">'
                        label = "cluster" if len(clusters) == 1 else "clusters"
                        content += f"<h5>{category} ({len(clusters)} {label})</h5>"
                        content += f"<p><strong>Recommendation:</strong> {clusters[0].get('Recommendation', 'Optimize cluster')}</p>"

                        savings_str = clusters[0].get("EstimatedSavings", "")
                        if savings_str:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

                        content += "<p><strong>Clusters:</strong></p><ul>"
                        for cluster in clusters:
                            cluster_id = cluster.get("ClusterId", "Unknown")
                            node_type = cluster.get("NodeType", "")
                            avg_cpu = cluster.get("AvgCPU")

                            display_str = cluster_id
                            if node_type:
                                display_str += f" ({node_type})"
                            if avg_cpu is not None:
                                display_str += f" - {avg_cpu}% CPU"

                            content += f"<li>{display_str}</li>"
                        content += "</ul></div>"
                    continue

                # Group OpenSearch by CheckCategory (Reserved instances, Graviton, storage optimization, etc.)
                if service_key == "opensearch":
                    grouped_opensearch = {}
                    for rec in recommendations:
                        # Group by optimization type for better organization
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_opensearch:
                            grouped_opensearch[category] = []
                        grouped_opensearch[category].append(rec)

                    for category, domains in grouped_opensearch.items():
                        if not domains:
                            continue

                        content += f'<div class="rec-item">'
                        label = "domain" if len(domains) == 1 else "domains"
                        content += f"<h5>{category} ({len(domains)} {label})</h5>"
                        content += f"<p><strong>Recommendation:</strong> {domains[0].get('Recommendation', 'Optimize domain')}</p>"

                        savings_str = domains[0].get("EstimatedSavings", "")
                        if savings_str:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

                        content += "<p><strong>Domains:</strong></p><ul>"
                        for domain in domains:
                            domain_name = domain.get("DomainName", "Unknown")
                            instance_type = domain.get("InstanceType", "")
                            avg_cpu = domain.get("AvgCPU")

                            display_str = domain_name
                            if instance_type:
                                display_str += f" ({instance_type})"
                            if avg_cpu is not None:
                                display_str += f" - {avg_cpu}% CPU"

                            content += f"<li>{display_str}</li>"
                        content += "</ul></div>"
                    continue

                # Group Network resources by CheckCategory (EIPs, NAT Gateways, Load Balancers, VPC endpoints)
                if service_key == "network":
                    grouped_network = {}
                    for rec in recommendations:
                        # Group by resource type for better organization
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_network:
                            grouped_network[category] = []
                        grouped_network[category].append(rec)

                    for category, resources in grouped_network.items():
                        if not resources:
                            continue

                        content += f'<div class="rec-item">'
                        content += f"<h5>{category} ({len(resources)} resources)</h5>"
                        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

                        savings_str = resources[0].get("EstimatedSavings", "")
                        if savings_str:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

                        content += "<p><strong>Resources:</strong></p><ul>"
                        for res in resources:
                            # Special handling for Duplicate VPC Endpoints - show individual endpoint IDs
                            if category == "Duplicate VPC Endpoints" and res.get("EndpointIds"):
                                service_name = (
                                    res.get("ServiceName", "").split(".")[-1] if res.get("ServiceName") else "unknown"
                                )
                                for endpoint_id in res.get("EndpointIds", []):
                                    content += f"<li>VPC Endpoint {endpoint_id} ({service_name})</li>"
                                continue

                            # Use ResourceName if available, otherwise fall back to technical IDs
                            resource_name = res.get("ResourceName")
                            if not resource_name:
                                resource_id = (
                                    res.get("AllocationId")
                                    or res.get("NatGatewayId")
                                    or res.get("LoadBalancerName")
                                    or res.get("VpcEndpointId")
                                    or res.get("VpcId")
                                    or res.get("AutoScalingGroupName")
                                    or res.get("InstanceId")  # Add EC2 instance support
                                    or (f"{res['ALBCount']} ALBs" if res.get("ALBCount") else None)
                                    or (
                                        f"{res['BackupPlanCount']} backup plans" if res.get("BackupPlanCount") else None
                                    )
                                    or "Unknown"
                                )

                                # Make technical IDs more readable
                                if resource_id.startswith("eipalloc-"):
                                    public_ip = res.get("PublicIp", "")
                                    resource_name = f"EIP {public_ip} ({resource_id})" if public_ip else resource_id
                                elif resource_id.startswith("i-"):  # EC2 instance
                                    instance_name = res.get("InstanceName", "Unknown")
                                    instance_type = res.get("InstanceType", "unknown")
                                    if instance_name != "Unknown":
                                        resource_name = f"{instance_name} ({instance_type})"
                                    else:
                                        resource_name = f"{instance_type} ({resource_id})"
                                elif resource_id.startswith("nat-"):
                                    az = res.get("AvailabilityZone", "")
                                    resource_name = f"NAT Gateway {resource_id} ({az})" if az else resource_id
                                elif resource_id.startswith("vpc-"):
                                    # For VPC endpoints, show service name if available
                                    if res.get("ServiceName"):
                                        service_name = res.get("ServiceName", "").split(".")[
                                            -1
                                        ]  # Get service name (e.g., 's3', 'ec2')
                                        resource_name = f"VPC {resource_id} ({service_name} endpoint)"
                                    else:
                                        resource_name = f"VPC {resource_id}"
                                elif resource_id.startswith("vpce-"):
                                    # For VPC endpoints, show service name with endpoint ID
                                    if res.get("ServiceName"):
                                        service_name = res.get("ServiceName", "").split(".")[
                                            -1
                                        ]  # Get service name (e.g., 's3', 'ec2')
                                        resource_name = f"VPC Endpoint {resource_id} ({service_name})"
                                    else:
                                        resource_name = f"VPC Endpoint {resource_id}"
                                elif resource_id.startswith("arn:aws:elasticloadbalancing"):
                                    # Extract load balancer name from ARN
                                    lb_name = resource_id.split("/")[-1] if "/" in resource_id else resource_id
                                    resource_name = f"Load Balancer {lb_name}"
                                else:
                                    resource_name = resource_id

                            content += f"<li>{resource_name}</li>"
                        content += "</ul></div>"
                    continue

                # Group Monitoring resources by CheckCategory (CloudWatch logs, CloudTrail, Backup, Route53)
                if service_key == "monitoring":
                    grouped_monitoring = {}
                    for rec in recommendations:
                        # Group by service type for better organization
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_monitoring:
                            grouped_monitoring[category] = []
                        grouped_monitoring[category].append(rec)

                    for category, resources in grouped_monitoring.items():
                        if not resources:
                            continue

                        content += f'<div class="rec-item">'
                        content += f"<h5>{category} ({len(resources)} resources)</h5>"
                        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

                        savings_str = resources[0].get("EstimatedSavings", "")
                        if savings_str:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

                        content += "<p><strong>Resources:</strong></p><ul>"
                        for res in resources:
                            # Handle TrailNames list for duplicate trails
                            if "TrailNames" in res and isinstance(res["TrailNames"], list):
                                resource_id = ", ".join(res["TrailNames"])
                            else:
                                resource_id = (
                                    res.get("LogGroupName")
                                    or res.get("TrailName")
                                    or res.get("AlarmName")
                                    or res.get("Namespace")
                                    or res.get("BackupPlanName")
                                    or res.get("HostedZoneId")
                                    or res.get("HealthCheckId")
                                    or (
                                        f"{res['BackupPlanCount']} backup plans" if res.get("BackupPlanCount") else None
                                    )
                                    or "Unknown"
                                )
                            content += f"<li>{resource_id}</li>"
                        content += "</ul></div>"
                    continue

                # File systems handled above - skip here
                if service_key == "file_systems":
                    continue

                # Group Additional Services by CheckCategory
                if service_key == "additional_services":
                    grouped_additional = {}
                    for rec in recommendations:
                        category = rec.get("CheckCategory", "Other")
                        if category not in grouped_additional:
                            grouped_additional[category] = []
                        grouped_additional[category].append(rec)

                    for category, resources in grouped_additional.items():
                        if not resources:
                            continue

                        content += f'<div class="rec-item">'
                        content += f"<h5>{category} ({len(resources)} resources)</h5>"
                        content += f"<p><strong>Recommendation:</strong> {resources[0].get('Recommendation', 'Optimize resource')}</p>"

                        savings_str = resources[0].get("EstimatedSavings", "")
                        if savings_str:
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {savings_str}</p>'

                        content += "<p><strong>Resources:</strong></p><ul>"
                        for res in resources:
                            resource_id = res.get(
                                "DistributionId",
                                res.get("ApiId", res.get("StateMachineArn", res.get("FunctionName", "Unknown"))),
                            )
                            if isinstance(resource_id, str) and ":" in resource_id:
                                resource_id = resource_id.split(":")[-1]
                            content += f"<li>{resource_id}</li>"
                        content += "</ul></div>"
                    continue

                # Skip services with full grouping - all sources are grouped above
                if service_key in [
                    "ebs",
                    "ec2",
                    "s3",
                    "dynamodb",
                    "containers",
                    "file_systems",
                    "network",
                    "monitoring",
                    "additional_services",
                ]:
                    continue

                # Show all recommendations for other sources
                for rec in recommendations:
                    # Skip spot-related recommendations
                    if "CheckCategory" in rec and "Spot" in rec.get("CheckCategory", ""):
                        continue
                    if "Recommendation" in rec and "spot instance" in rec.get("Recommendation", "").lower():
                        continue

                    # Skip optimized resources from Cost Optimization Hub and Compute Optimizer
                    finding = rec.get("finding", rec.get("instanceFinding", rec.get("InstanceFinding", ""))).lower()
                    if finding == "optimized":
                        continue

                    content += f'<div class="rec-item">'

                    if service_key == "ec2":
                        # Enhanced EC2 recommendations with check categories
                        if "CheckCategory" in rec:
                            content += f"<h5>{rec.get('CheckCategory', 'EC2 Optimization')}: {rec.get('InstanceId', rec.get('ImageId', rec.get('AllocationId', 'Resource')))}</h5>"
                            if "InstanceType" in rec:
                                content += f"<p><strong>Instance Type:</strong> {rec.get('InstanceType')}</p>"
                            if "CurrentType" in rec:
                                content += f"<p><strong>Current:</strong> {rec.get('CurrentType')} → <strong>Recommended:</strong> {rec.get('RecommendedType')}</p>"
                            if "PublicIp" in rec:
                                content += f"<p><strong>Elastic IP:</strong> {rec.get('PublicIp')}</p>"
                            if "AgeDays" in rec:
                                content += f"<p><strong>Age:</strong> {rec.get('AgeDays')} days</p>"
                            content += f"<p><strong>Recommendation:</strong> {rec.get('Recommendation', 'Optimize resource')}</p>"
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec.get("EstimatedSavings", "Cost optimization")}</p>'
                        else:
                            # Original EC2 format
                            if "actionType" in rec:
                                # Cost Optimization Hub format
                                content += f"<h5>Resource: {rec.get('resourceId', 'N/A')}</h5>"
                                content += f"<p><strong>Action:</strong> {rec.get('actionType', 'N/A')}</p>"
                                content += f'<p class="savings"><strong>Monthly Savings:</strong> ${rec.get("estimatedMonthlySavings", 0):.2f}</p>'

                                # Extract current instance type from nested structure
                                current_type = (
                                    rec.get("currentResourceDetails", {})
                                    .get("ec2Instance", {})
                                    .get("configuration", {})
                                    .get("instance", {})
                                    .get("type", "N/A")
                                )
                                if current_type != "N/A":
                                    content += f"<p><strong>Current Type:</strong> {current_type}</p>"

                                # Extract recommended instance type
                                rec_type = (
                                    rec.get("recommendedResourceDetails", {})
                                    .get("ec2Instance", {})
                                    .get("configuration", {})
                                    .get("instance", {})
                                    .get("type", "N/A")
                                )
                                if rec_type != "N/A":
                                    content += f"<p>Recommended Type: {rec_type}</p>"
                            elif "instanceArn" in rec:
                                # Compute Optimizer format
                                instance_name = rec.get("instanceName", "N/A")
                                instance_id = (
                                    rec.get("instanceArn", "").split("/")[-1] if rec.get("instanceArn") else "N/A"
                                )
                                content += f"<h5>Instance: {instance_name or instance_id}</h5>"
                                content += f"<p><strong>Finding:</strong> {rec.get('finding', 'N/A')}</p>"
                                content += (
                                    f"<p><strong>Current Type:</strong> {rec.get('currentInstanceType', 'N/A')}</p>"
                                )

                            # Utilization metrics removed - now grouped by finding

                    elif service_key == "ebs":
                        # Enhanced EBS recommendations with check categories
                        if "CheckCategory" in rec:
                            content += f"<h5>{rec.get('CheckCategory', 'EBS Optimization')}: {rec.get('VolumeId', rec.get('SnapshotId', 'Resource'))}</h5>"
                            if "Size" in rec:
                                content += f"<p><strong>Size:</strong> {rec.get('Size')} GB</p>"
                            if "CurrentType" in rec:
                                content += f"<p><strong>Migration:</strong> {rec.get('CurrentType')} → {rec.get('RecommendedType')}</p>"
                            if "CurrentIOPS" in rec:
                                content += f"<p><strong>IOPS:</strong> {rec.get('CurrentIOPS')} → {rec.get('RecommendedIOPS')} (recommended)</p>"
                            if "AgeDays" in rec:
                                content += f"<p><strong>Age:</strong> {rec.get('AgeDays')} days</p>"
                            content += f"<p><strong>Recommendation:</strong> {rec.get('Recommendation', 'Optimize resource')}</p>"
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> {rec.get("EstimatedSavings", "Cost optimization")}</p>'
                        else:
                            # Cost Optimization Hub EBS format
                            if "actionType" in rec and "ebsVolume" in rec.get("currentResourceDetails", {}):
                                content += f"<h5>Resource: {rec.get('resourceId', 'N/A')}</h5>"
                                content += f"<p><strong>Action:</strong> {rec.get('actionType', 'N/A')}</p>"
                                content += f'<p class="savings"><strong>Monthly Savings:</strong> ${rec.get("estimatedMonthlySavings", 0):.2f}</p>'

                                # Extract current volume configuration
                                ebs_config = (
                                    rec.get("currentResourceDetails", {}).get("ebsVolume", {}).get("configuration", {})
                                )
                                storage = ebs_config.get("storage", {})
                                current_type = storage.get("type", "N/A")
                                current_size = storage.get("sizeInGb", 0)

                                if current_type != "N/A":
                                    content += f"<p><strong>Current:</strong> {current_type} ({current_size} GB)</p>"

                                # Extract recommended volume configuration
                                rec_ebs_config = (
                                    rec.get("recommendedResourceDetails", {})
                                    .get("ebsVolume", {})
                                    .get("configuration", {})
                                )
                                rec_storage = rec_ebs_config.get("storage", {})
                                rec_type = rec_storage.get("type", "N/A")
                                rec_size = rec_storage.get("sizeInGb", 0)

                                if rec_type != "N/A":
                                    content += f"<p>Recommended: {rec_type} ({rec_size} GB)</p>"
                            # Original EBS format
                            elif "VolumeId" in rec:  # Unattached volume
                                content += f"<h5>Volume: {rec.get('VolumeId', 'N/A')}</h5>"
                                content += f"<p>Type: {rec.get('VolumeType', 'N/A')} - {rec.get('Size', 0)} GB</p>"
                                content += (
                                    f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'
                                )
                                content += f"<p><strong>Recommended Action:</strong> Delete unattached volume (create snapshot first if needed)</p>"
                            else:  # Compute Optimizer recommendation
                                volume_id = (
                                    rec.get("volumeArn", "N/A").split("/")[-1] if rec.get("volumeArn") else "N/A"
                                )
                                content += f"<h5>Volume: {volume_id}</h5>"
                                content += f"<p>Finding: {rec.get('finding', 'N/A')}</p>"

                                # Show current configuration
                                current_config = rec.get("currentConfiguration", {})
                                content += f"<p>Current: {current_config.get('volumeType', 'N/A')} - {current_config.get('volumeSize', 0)} GB"
                                if current_config.get("volumeBaselineIOPS"):
                                    content += f" - {current_config.get('volumeBaselineIOPS', 0)} IOPS"
                                if current_config.get("volumeBaselineThroughput"):
                                    content += f" - {current_config.get('volumeBaselineThroughput', 0)} MB/s"
                                content += "</p>"

                                # Show recommended actions
                                if rec.get("volumeRecommendationOptions"):
                                    content += "<p><strong>Recommended Actions:</strong></p><ul>"
                                    for i, option in enumerate(rec["volumeRecommendationOptions"][:2], 1):
                                        config = option.get("configuration", {})
                                        risk = option.get("performanceRisk", 0)

                                        action_desc = f"Option {i}: "
                                        changes = []

                                        if config.get("volumeType") != current_config.get("volumeType"):
                                            changes.append(f"Change type to {config.get('volumeType', 'N/A')}")
                                        if config.get("volumeSize") != current_config.get("volumeSize"):
                                            changes.append(f"Resize to {config.get('volumeSize', 0)} GB")
                                        if config.get("volumeBaselineIOPS") != current_config.get("volumeBaselineIOPS"):
                                            changes.append(f"Adjust IOPS to {config.get('volumeBaselineIOPS', 0)}")
                                        if config.get("volumeBaselineThroughput") != current_config.get(
                                            "volumeBaselineThroughput"
                                        ):
                                            changes.append(
                                                f"Adjust throughput to {config.get('volumeBaselineThroughput', 0)} MB/s"
                                            )

                                        if changes:
                                            action_desc += ", ".join(changes)
                                        else:
                                            action_desc += "Optimize configuration"

                                        action_desc += f" (Performance Risk: {risk})"
                                        content += f"<li>{action_desc}</li>"
                                    content += "</ul>"

                    elif service_key == "rds":
                        # Skip if no actionable findings
                        instance_finding = rec.get("instanceFinding", "N/A")
                        storage_finding = rec.get("storageFinding", "N/A")
                        has_recommendations = rec.get("instanceRecommendationOptions") or rec.get(
                            "storageRecommendationOptions"
                        )

                        # Skip if both findings are N/A and no recommendations
                        if instance_finding == "N/A" and storage_finding == "N/A" and not has_recommendations:
                            continue

                        resource_arn = rec.get("resourceArn", "N/A")
                        db_name = resource_arn.split(":")[-1] if resource_arn != "N/A" else "N/A"
                        content += f"<h5>Database: {db_name}</h5>"
                        content += (
                            f"<p><strong>Engine:</strong> {rec.get('engine', 'N/A')} {rec.get('engineVersion', '')}</p>"
                        )

                        # Instance findings and recommendations
                        if instance_finding != "N/A":
                            content += f"<p><strong>Instance Finding:</strong> {instance_finding}</p>"

                        if rec.get("instanceRecommendationOptions"):
                            content += "<p><strong>Instance Recommendations:</strong></p><ul>"
                            current_class = rec.get("dbInstanceClass", "N/A")
                            content += f"<li>Current: {current_class}</li>"

                            for i, option in enumerate(rec["instanceRecommendationOptions"][:2], 1):
                                recommended_class = option.get("dbInstanceClass", "N/A")
                                rank = option.get("rank", i)
                                content += f"<li>Option {rank}: Migrate to {recommended_class}</li>"
                            content += "</ul>"

                        # Storage findings and recommendations
                        if storage_finding != "N/A":
                            content += f"<p><strong>Storage Finding:</strong> {storage_finding}</p>"

                            if rec.get("storageRecommendationOptions"):
                                content += "<p><strong>Storage Recommendations:</strong></p><ul>"
                                for option in rec["storageRecommendationOptions"][:1]:
                                    storage_config = option.get("storageConfiguration", {})
                                    storage_type = storage_config.get("storageType", "N/A")
                                    allocated_storage = storage_config.get("allocatedStorage", "N/A")
                                    iops = storage_config.get("iops", "N/A")
                                    content += f"<li>Optimize to: {storage_type}"
                                    if allocated_storage != "N/A":
                                        content += f" - {allocated_storage} GB"
                                    if iops != "N/A":
                                        content += f" - {iops} IOPS"
                                    content += "</li>"
                                content += "</ul>"

                        # Show utilization metrics if available
                        if rec.get("utilizationMetrics"):
                            content += "<p><strong>Current Utilization:</strong></p><ul>"
                            for metric in rec["utilizationMetrics"][:3]:  # Show top 3 metrics
                                metric_name = metric.get("name", "N/A")
                                metric_value = metric.get("value", 0)
                                statistic = metric.get("statistic", "N/A")
                                content += f"<li>{metric_name} ({statistic}): {metric_value:.2f}</li>"
                            content += "</ul>"

                    elif service_key == "file_systems":
                        if "FileSystemId" in rec and rec.get("FileSystemType"):  # FSx
                            fs_id = rec.get("FileSystemId", "N/A")
                            fs_type = rec.get("FileSystemType", "N/A")
                            content += f"<h5>FSx {fs_type}: {fs_id}</h5>"
                            content += f"<p>Capacity: {rec.get('StorageCapacity', 0)} GB</p>"
                            content += f"<p>Storage Type: {rec.get('StorageType', 'N/A')}</p>"
                            content += f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'

                            # Show specific optimization opportunities
                            opportunities = rec.get("OptimizationOpportunities", [])
                            if opportunities:
                                content += "<p><strong>Recommended Actions:</strong></p><ul>"
                                for opp in opportunities:
                                    content += f"<li>{opp}</li>"
                                content += "</ul>"

                            # Add type-specific recommendations with cost estimates
                            potential_savings = rec.get("EstimatedMonthlyCost", 0) * 0.3
                            if fs_type.upper() == "ONTAP":
                                content += "<p><strong>ONTAP Optimizations:</strong></p><ul>"
                                content += f"<li>Enable data deduplication and compression (Save ~${potential_savings * 0.5:.2f}/month)</li>"
                                content += f"<li>Configure capacity pool for cold data (Save ~${potential_savings * 0.3:.2f}/month)</li>"
                                content += "<li>Use SnapMirror for efficient replication</li>"
                                content += "</ul>"
                            elif fs_type.upper() == "LUSTRE":
                                content += "<p><strong>Lustre Optimizations:</strong></p><ul>"
                                content += f"<li>Consider scratch file systems for temporary workloads (Save ~${potential_savings * 0.6:.2f}/month)</li>"
                                content += (
                                    f"<li>Enable LZ4 data compression (Save ~${potential_savings * 0.2:.2f}/month)</li>"
                                )
                                content += "<li>Optimize metadata configuration</li>"
                                content += "</ul>"
                            elif fs_type.upper() == "OPENZFS":
                                content += "<p><strong>OpenZFS Optimizations:</strong></p><ul>"
                                content += (
                                    f"<li>Enable Intelligent-Tiering (Save ~${potential_savings * 0.5:.2f}/month)</li>"
                                )
                                content += "<li>Use zero-copy snapshots and clones</li>"
                                content += "<li>Configure user/group quotas</li>"
                                content += "</ul>"

                        else:  # EFS
                            fs_name = rec.get("Name", rec.get("FileSystemId", "N/A"))
                            content += f"<h5>EFS: {fs_name}</h5>"
                            content += f"<p>Size: {rec.get('SizeGB', 0)} GB</p>"
                            content += f"<p>Storage Class: {rec.get('StorageClass', 'N/A')}</p>"
                            content += f"<p>Mount Targets: {rec.get('MountTargets', 0)}</p>"
                            content += f'<p class="savings">Monthly Cost: ${rec.get("EstimatedMonthlyCost", 0):.2f}</p>'

                            # Show specific EFS recommendations with cost calculations
                            content += "<p><strong>Recommended Actions:</strong></p><ul>"

                            if not rec.get("HasIAPolicy", True):
                                ia_savings = rec.get("EstimatedMonthlyCost", 0) * 0.8
                                content += (
                                    f"<li>Enable Transition to IA after 30 days (Save ~${ia_savings:.2f}/month)</li>"
                                )

                            if not rec.get("HasArchivePolicy", True):
                                archive_savings = rec.get("EstimatedMonthlyCost", 0) * 0.9
                                content += f"<li>Enable Transition to Archive after 90 days (Save ~${archive_savings:.2f}/month)</li>"

                            if rec.get("StorageClass") == "Standard" and rec.get("SizeGB", 0) > 1:
                                one_zone_savings = rec.get("EstimatedMonthlyCost", 0) * 0.47
                                content += f"<li>Consider One Zone storage if Multi-AZ not required (Save ~${one_zone_savings:.2f}/month)</li>"

                            if rec.get("MountTargets", 0) == 0 and rec.get("SizeGB", 0) < 0.1:
                                content += f"<li>Delete unused file system (Save ${rec.get('EstimatedMonthlyCost', 0):.2f}/month)</li>"

                            content += "</ul>"

                    elif service_key == "s3":
                        # Skip buckets with no meaningful data
                        bucket_name = rec.get("Name") or rec.get("BucketName", "Unknown")
                        bucket_size = rec.get("SizeGB", 0)
                        bucket_cost = rec.get("EstimatedMonthlyCost", 0)

                        # Skip recommendations without valid bucket names
                        if bucket_name == "Unknown" or not bucket_name:
                            continue

                        if bucket_name == "Unknown" and bucket_size == 0 and bucket_cost == 0:
                            continue

                        # S3 bucket recommendations
                        content += f"<h5>S3 Bucket: {bucket_name}</h5>"
                        content += f"<p><strong>Size:</strong> {bucket_size:.2f} GB</p>"
                        content += f"<p><strong>Monthly Cost:</strong> ${bucket_cost:.2f}</p>"
                        content += f"<p><strong>Created:</strong> {rec.get('CreationDate', 'Unknown')}</p>"
                        content += f"<p><strong>Lifecycle Policy:</strong> {'Yes' if rec.get('HasLifecyclePolicy') else 'No'}</p>"
                        content += f"<p><strong>Intelligent Tiering:</strong> {'Yes' if rec.get('HasIntelligentTiering') else 'No'}</p>"

                        # Show optimization opportunities
                        opportunities = rec.get("OptimizationOpportunities", [])
                        if opportunities:
                            content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
                            for opp in opportunities:
                                content += f"<li>{opp}</li>"
                            content += "</ul>"

                            # Add specific recommendations based on missing features
                            if not rec.get("HasLifecyclePolicy"):
                                content += "<p><strong>Lifecycle Policy Benefits:</strong></p><ul>"
                                content += "<li>Transition to Standard-IA after 30 days (Save 40%)</li>"
                                content += "<li>Transition to Glacier after 90 days (Save 68%)</li>"
                                content += "<li>Transition to Deep Archive after 180 days (Save 95%)</li>"
                                content += "</ul>"

                            if not rec.get("HasIntelligentTiering"):
                                content += "<p><strong>Intelligent Tiering Benefits:</strong></p><ul>"
                                content += "<li>Automatic optimization based on access patterns</li>"
                                content += "<li>Archive tiers for long-term storage (Save up to 95%)</li>"
                                content += "<li>Small monitoring fee ($0.0025 per 1,000 objects)</li>"
                                content += "</ul>"
                    elif service_key == "dynamodb":
                        # DynamoDB table recommendations
                        content += f"<h5>DynamoDB Table: {rec.get('TableName', 'Unknown')}</h5>"
                        content += f"<p><strong>Billing Mode:</strong> {rec.get('BillingMode', 'Unknown')}</p>"
                        content += f"<p><strong>Status:</strong> {rec.get('TableStatus', 'Unknown')}</p>"
                        content += f"<p><strong>Item Count:</strong> {rec.get('ItemCount', 0):,}</p>"
                        content += (
                            f"<p><strong>Table Size:</strong> {rec.get('TableSizeBytes', 0) / (1024**2):.2f} MB</p>"
                        )

                        if rec.get("BillingMode") == "PROVISIONED":
                            content += f"<p><strong>Read Capacity:</strong> {rec.get('ReadCapacityUnits', 0)} RCU</p>"
                            content += f"<p><strong>Write Capacity:</strong> {rec.get('WriteCapacityUnits', 0)} WCU</p>"
                            content += (
                                f"<p><strong>Monthly Cost:</strong> ${rec.get('EstimatedMonthlyCost', 0):.2f}</p>"
                            )

                        # Show optimization opportunities
                        opportunities = rec.get("OptimizationOpportunities", [])
                        if opportunities:
                            content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
                            for opp in opportunities:
                                content += f"<li>{opp}</li>"
                            content += "</ul>"

                            # Add specific recommendations based on billing mode
                            if rec.get("BillingMode") == "PROVISIONED":
                                content += "<p><strong>Provisioned Mode Optimizations:</strong></p><ul>"
                                content += "<li>Enable Auto Scaling for dynamic capacity adjustment</li>"
                                content += "<li>Monitor consumed vs provisioned capacity</li>"
                                content += "<li>Consider Reserved Capacity for steady workloads (Save 53-76%)</li>"
                                content += "</ul>"
                            else:
                                content += "<p><strong>On-Demand Mode Considerations:</strong></p><ul>"
                                content += "<li>Monitor request patterns for potential Provisioned savings</li>"
                                content += "<li>Implement efficient access patterns</li>"
                                content += "<li>Consider Provisioned mode if usage is predictable</li>"
                                content += "</ul>"
                    elif service_key == "containers":
                        # Container services recommendations
                        if "ClusterName" in rec:  # ECS or EKS cluster
                            if "Version" in rec:  # EKS cluster
                                content += f"<h5>EKS Cluster: {rec.get('ClusterName', 'Unknown')}</h5>"
                                content += f"<p><strong>Version:</strong> {rec.get('Version', 'Unknown')}</p>"
                                content += f"<p><strong>Node Groups:</strong> {rec.get('NodeGroupsCount', 0)}</p>"
                                content += (
                                    f"<p><strong>Monthly Cost:</strong> ${rec.get('EstimatedMonthlyCost', 0):.2f}</p>"
                                )
                            else:  # ECS cluster
                                content += f"<h5>ECS Cluster: {rec.get('ClusterName', 'Unknown')}</h5>"
                                content += f"<p><strong>Running Tasks:</strong> {rec.get('RunningTasksCount', 0)}</p>"
                                content += f"<p><strong>Services:</strong> {rec.get('ServicesCount', 0)}</p>"

                            content += f"<p><strong>Status:</strong> {rec.get('Status', 'Unknown')}</p>"

                        elif "RepositoryName" in rec:  # ECR repository
                            content += f"<h5>ECR Repository: {rec.get('RepositoryName', 'Unknown')}</h5>"
                            content += f"<p><strong>Images:</strong> {rec.get('ImageCount', 0)}</p>"
                            content += f"<p><strong>Created:</strong> {rec.get('CreatedAt', 'Unknown')}</p>"

                        # Show optimization opportunities
                        opportunities = rec.get("OptimizationOpportunities", [])
                        if opportunities:
                            content += "<p><strong>Optimization Opportunities:</strong></p><ul>"
                            for opp in opportunities:
                                content += f"<li>{opp}</li>"
                            content += "</ul>"

                    elif service_key == "lambda":
                        # Lambda function recommendations - handle both Cost Optimization Hub and enhanced checks
                        function_name = rec.get("FunctionName") or rec.get("resourceId", "Unknown")
                        check_category = rec.get("CheckCategory", "Lambda Optimization")

                        # For Cost Optimization Hub recommendations, use actionType as category
                        if "actionType" in rec:
                            check_category = f"Lambda {rec['actionType']}"

                        content += f"<h5>{check_category}: {function_name}</h5>"

                        # Display function details - handle both formats
                        if "MemorySize" in rec:
                            content += f"<p><strong>Memory Size:</strong> {rec['MemorySize']} MB</p>"
                        elif "currentResourceDetails" in rec:
                            lambda_config = (
                                rec.get("currentResourceDetails", {}).get("lambdaFunction", {}).get("configuration", {})
                            )
                            compute_config = lambda_config.get("compute", {})
                            if "memorySizeInMB" in compute_config:
                                content += f"<p><strong>Memory Size:</strong> {compute_config['memorySizeInMB']} MB</p>"
                            if "architecture" in compute_config:
                                content += f"<p><strong>Architecture:</strong> {compute_config['architecture']}</p>"

                        if "Timeout" in rec:
                            content += f"<p><strong>Timeout:</strong> {rec['Timeout']} seconds</p>"
                        if "Runtime" in rec:
                            content += f"<p><strong>Runtime:</strong> {rec['Runtime']}</p>"
                        if "Architecture" in rec:
                            content += f"<p><strong>Architecture:</strong> {rec['Architecture']}</p>"

                        # Show recommendation - handle both formats
                        if "Recommendation" in rec:
                            content += f"<p><strong>Recommendation:</strong> {rec['Recommendation']}</p>"
                        elif "actionType" in rec:
                            # Generate recommendation based on Cost Optimization Hub actionType
                            if rec["actionType"] == "Rightsize":
                                content += f"<p><strong>Recommendation:</strong> Right-size Lambda function memory allocation based on usage patterns</p>"
                            else:
                                content += f"<p><strong>Recommendation:</strong> {rec['actionType']} Lambda function for cost optimization</p>"

                        # Show estimated savings
                        if "EstimatedSavings" in rec:
                            content += (
                                f'<p class="savings"><strong>Estimated Savings:</strong> {rec["EstimatedSavings"]}</p>'
                            )
                        elif "estimatedMonthlySavings" in rec:
                            monthly_savings = rec["estimatedMonthlySavings"]
                            savings_pct = rec.get("estimatedSavingsPercentage", 0)
                            content += f'<p class="savings"><strong>Estimated Savings:</strong> ${monthly_savings:.2f}/month ({savings_pct:.1f}%)</p>'

                    else:
                        # Generic handler for all other services (network, monitoring, etc.)
                        # Display check category as title
                        check_category = rec.get("CheckCategory", source_name.replace("_", " ").title())
                        # Extract resource identifier with special handling for arrays and counts
                        resource_id = (
                            rec.get("LoadBalancerName")
                            or rec.get("AutoScalingGroupName")
                            or rec.get("VpcEndpointId")
                            or rec.get("NatGatewayId")
                            or rec.get("AllocationId")
                            or rec.get("LogGroupName")
                            or rec.get("TrailName")
                            or rec.get("FunctionName")
                            or rec.get("DistributionId")
                            or rec.get("ApiId")
                            or rec.get("VpcId")
                            or rec.get("StateMachineArn", "").split(":")[-1]
                            or rec.get("BackupPlanName")
                            or rec.get("BackupVaultName")
                            or rec.get("HostedZoneId")
                            or rec.get("HealthCheckId")
                            or rec.get("GroupName")
                            or rec.get("PlanName")
                            or rec.get("ResourceId")
                            or rec.get("SnapshotId")
                            or rec.get("dbClusterIdentifier")
                            or rec.get("dbInstanceIdentifier")
                            or (f"{rec['BackupPlanCount']} backup plans" if rec.get("BackupPlanCount") else None)
                            or (f"{rec['ALBCount']} ALBs" if rec.get("ALBCount") else None)
                            or rec.get("resourceArn", "").split(":")[-1]
                            if rec.get("resourceArn")
                            else "Resource"
                        )

                        content += f"<h5>{check_category}: {resource_id}</h5>"

                        # Display all relevant fields
                        for key, value in rec.items():
                            if key not in ["CheckCategory", "Recommendation", "EstimatedSavings"] and not key.endswith(
                                "Arn"
                            ):
                                if isinstance(value, (str, int, float)) and value:
                                    formatted_key = key.replace("_", " ").title()
                                    content += f"<p><strong>{formatted_key}:</strong> {value}</p>"

                        # Show recommendation
                        if "Recommendation" in rec:
                            content += f"<p><strong>Recommendation:</strong> {rec['Recommendation']}</p>"

                        # Show estimated savings
                        if "EstimatedSavings" in rec:
                            content += (
                                f'<p class="savings"><strong>Estimated Savings:</strong> {rec["EstimatedSavings"]}</p>'
                            )

                    content += "</div>"

                # All items are now shown, no need for show more button

        # Add top 10 buckets section for S3
        if service_key == "s3":
            sources = service_data.get("sources", {})
            s3_data = sources.get("s3_bucket_analysis", {})

            # Top 10 by cost
            top_cost = s3_data.get("top_cost_buckets", [])
            if top_cost:
                content += "<h4>Top 10 Buckets by Monthly Cost</h4>"
                content += '<div class="top-buckets-table">'
                content += "<table><tr><th>Bucket Name</th><th>Size (GB)</th><th>Monthly Cost</th><th>Lifecycle</th><th>Intelligent Tiering</th></tr>"
                for bucket in top_cost:
                    content += f"<tr>"
                    content += f"<td>{bucket.get('Name', 'N/A')}</td>"
                    content += f"<td>{bucket.get('SizeGB', 0):.2f}</td>"
                    content += f"<td>${bucket.get('EstimatedMonthlyCost', 0):.2f}</td>"
                    content += f"<td>{'✓' if bucket.get('HasLifecyclePolicy') else '✗'}</td>"
                    content += f"<td>{'✓' if bucket.get('HasIntelligentTiering') else '✗'}</td>"
                    content += f"</tr>"
                content += "</table></div>"

            # Top 10 by size
            top_size = s3_data.get("top_size_buckets", [])
            if top_size:
                content += "<h4>Top 10 Buckets by Size</h4>"
                content += '<div class="top-buckets-table">'
                content += "<table><tr><th>Bucket Name</th><th>Size (GB)</th><th>Monthly Cost</th><th>Lifecycle</th><th>Intelligent Tiering</th></tr>"
                for bucket in top_size:
                    content += f"<tr>"
                    content += f"<td>{bucket.get('Name', 'N/A')}</td>"
                    content += f"<td>{bucket.get('SizeGB', 0):.2f}</td>"
                    content += f"<td>${bucket.get('EstimatedMonthlyCost', 0):.2f}</td>"
                    content += f"<td>{'✓' if bucket.get('HasLifecyclePolicy') else '✗'}</td>"
                    content += f"<td>{'✓' if bucket.get('HasIntelligentTiering') else '✗'}</td>"
                    content += f"</tr>"
                content += "</table></div>"

        content += "</div>"
        return content

    def _get_footer(self) -> str:
        """Get footer section"""
        return f"""<div class="footer">
            <p>Generated by AWS Cost Optimization Scanner on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
            <p>Report covers {self.scan_results["summary"]["total_services_scanned"]} AWS services with {self.scan_results["summary"]["total_recommendations"]} optimization recommendations</p>
        </div>"""

    def _get_savings_plans_content(self, sp_data: Dict[str, Any]) -> str:
        """Generate Savings Plans analysis content"""
        summary = sp_data.get("summary", {})
        active_plans = sp_data.get("active_plans", [])
        utilization = sp_data.get("utilization_analysis", {})
        coverage = sp_data.get("coverage_analysis", {})
        recommendations = sp_data.get("recommendations", [])
        uncovered_families = sp_data.get("uncovered_families", [])

        content = '<div class="service-header">'
        content += '<h2 class="service-title">Savings Plans Cost Optimization</h2>'
        content += '<div class="service-stats">'
        content += f'<div class="stat-card"><h4>Active Plans</h4><div class="value">{summary.get("total_active_plans", 0)}</div></div>'
        content += f'<div class="stat-card"><h4>Total Commitment</h4><div class="value">${summary.get("total_commitment", 0):.2f}/hr</div></div>'

        if utilization:
            util_pct = utilization.get("utilization_percentage", 0)
            util_status = utilization.get("status", "Unknown")
            status_class = (
                "success" if util_status == "Good" else "warning" if util_status == "Needs Attention" else "danger"
            )
            content += f'<div class="stat-card"><h4>Utilization</h4><div class="value {status_class}">{util_pct:.1f}%</div></div>'

        if coverage:
            cov_pct = coverage.get("coverage_percentage", 0)
            cov_status = coverage.get("status", "Unknown")
            status_class = "success" if cov_status == "Good" else "warning" if cov_status == "Moderate" else "danger"
            content += (
                f'<div class="stat-card"><h4>Coverage</h4><div class="value {status_class}">{cov_pct:.1f}%</div></div>'
            )

        content += "</div></div>"

        # Active Plans Section
        if active_plans:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Active Savings Plans</h3>'
            for plan in active_plans:
                plan_type = plan.get("savingsPlanType", "Unknown")
                commitment = plan.get("commitment", 0)
                region = plan.get("region", "N/A")
                family = plan.get("ec2InstanceFamily", "N/A")
                payment = plan.get("paymentOption", "N/A")

                content += '<div class="rec-item">'
                content += f"<h5>{plan_type} Savings Plan</h5>"
                content += f"<p><strong>Commitment:</strong> ${commitment:.2f}/hour</p>"
                if plan_type == "EC2Instance":
                    content += f"<p><strong>Instance Family:</strong> {family} | <strong>Region:</strong> {region}</p>"
                content += f"<p><strong>Payment Option:</strong> {payment}</p>"
                content += f"<p><strong>Term:</strong> {plan.get('start', 'N/A')} to {plan.get('end', 'N/A')}</p>"
                content += "</div>"
            content += "</div>"

        # Utilization Analysis
        if utilization:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Utilization Analysis</h3>'
            content += '<div class="rec-item">'
            content += f"<h5>Overall Utilization: {utilization.get('utilization_percentage', 0):.1f}% ({utilization.get('status', 'Unknown')})</h5>"
            content += f"<p><strong>Total Commitment:</strong> ${utilization.get('total_commitment', '0')}</p>"
            content += f"<p><strong>Used Commitment:</strong> ${utilization.get('used_commitment', '0')}</p>"
            content += f'<p><strong>Unused Commitment:</strong> <span class="danger">${utilization.get("unused_commitment", "0")}</span></p>'
            content += "</div></div>"

        # Coverage Analysis
        if coverage:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Coverage Analysis</h3>'
            content += '<div class="rec-item">'
            content += f"<h5>Coverage: {coverage.get('coverage_percentage', 0):.1f}% ({coverage.get('status', 'Unknown')})</h5>"
            content += f"<p><strong>On-Demand Cost:</strong> ${coverage.get('on_demand_cost', 0):.2f}</p>"
            content += f"<p><strong>Spend Covered by Savings Plans:</strong> ${coverage.get('spend_covered', '0')}</p>"
            content += f"<p><strong>Total Cost:</strong> ${coverage.get('total_cost', '0')}</p>"
            content += "</div></div>"

        # Recommendations
        if recommendations:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Optimization Recommendations</h3>'
            for rec in recommendations:
                severity = rec.get("severity", "Medium")
                badge_class = "danger" if severity == "High" else "warning" if severity == "Medium" else "success"

                content += f'<div class="rec-item">'
                content += f'<h5>{rec.get("type", "Recommendation")} <span class="badge badge-{badge_class}">{severity}</span></h5>'
                content += f"<p><strong>Finding:</strong> {rec.get('finding', 'N/A')}</p>"
                content += f"<p><strong>Recommendation:</strong> {rec.get('recommendation', 'N/A')}</p>"
                if "potential_monthly_savings" in rec:
                    content += (
                        f'<p class="savings"><strong>Potential Savings:</strong> {rec["potential_monthly_savings"]}</p>'
                    )
                if "potential_waste" in rec:
                    content += f'<p class="danger"><strong>Potential Waste:</strong> ${rec["potential_waste"]}</p>'
                content += "</div>"
            content += "</div>"

        # Uncovered Instance Families
        if uncovered_families:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Uncovered Instance Families</h3>'
            content += "<p>The following instance families are not covered by EC2 Instance Savings Plans:</p>"
            for family_info in uncovered_families:
                content += '<div class="rec-item">'
                content += f"<h5>{family_info.get('family', 'Unknown')} Family</h5>"
                content += f"<p><strong>Instance Count:</strong> {family_info.get('instance_count', 0)}</p>"
                content += f"<p><strong>Instance Types:</strong> {', '.join(family_info.get('instance_types', []))}</p>"
                content += f"<p><strong>Recommendation:</strong> {family_info.get('recommendation', 'N/A')}</p>"
                content += f'<p class="savings"><strong>Estimated Savings:</strong> {family_info.get("estimated_savings", "N/A")}</p>'
                content += "</div>"
            content += "</div>"

        # Cost Optimization Hub Purchase Recommendations
        cost_hub_recs = sp_data.get("cost_hub_purchase_recommendations", [])
        if cost_hub_recs:
            content += '<div class="recommendation-section">'
            content += '<h3 class="section-title">💡 Cost Optimization Hub - Purchase Recommendations</h3>'
            content += "<p>AWS Cost Optimization Hub has identified opportunities to purchase Savings Plans:</p>"
            for rec in cost_hub_recs:
                severity = rec.get("severity", "Medium")
                badge_class = "danger" if severity == "High" else "warning" if severity == "Medium" else "success"

                content += f'<div class="rec-item">'
                content += f'<h5>{rec.get("type", "Recommendation")} <span class="badge badge-{badge_class}">{severity}</span></h5>'
                content += f"<p><strong>Recommendation:</strong> {rec.get('recommendation', 'N/A')}</p>"
                content += f'<p class="savings"><strong>Potential Monthly Savings:</strong> {rec.get("potential_monthly_savings", "N/A")} ({rec.get("savings_percentage", "N/A")})</p>'
                content += f"<p><strong>Implementation Effort:</strong> {rec.get('implementation_effort', 'N/A')}</p>"
                content += f'<p class="source-info"><strong>Source:</strong> {rec.get("source", "N/A")}</p>'
                content += "</div>"
            content += "</div>"

        # No Savings Plans message
        if not summary.get("has_savings_plans"):
            content += '<div class="info-box">'
            content += '<h3 class="section-title">ℹ️ No Active Savings Plans Found</h3>'
            content += "<p>Consider purchasing Savings Plans to save up to 72% on your compute usage:</p>"
            content += "<ul>"
            content += "<li><strong>Compute Savings Plans:</strong> Most flexible, up to 66% savings on EC2, Fargate, and Lambda</li>"
            content += "<li><strong>EC2 Instance Savings Plans:</strong> Highest savings (up to 72%) for specific instance families</li>"
            content += "</ul>"

            # Show Cost Hub recommendations even without active plans
            if cost_hub_recs:
                content += '<p class="callout-margin"><strong>💡 Cost Optimization Hub has identified purchase opportunities above.</strong></p>'

            content += "</div>"

        return content

    def _get_javascript(self) -> str:
        """Get JavaScript for interactivity"""
        # Extract chart data from scan results
        services = self.scan_results["services"]
        chart_data = []

        for service_key, service_data in services.items():
            if service_data.get("total_recommendations", 0) > 0:
                chart_data.append(
                    {
                        "service": service_data.get("service_name", service_key.title()),
                        "service_key": service_key,
                        "savings": service_data.get("total_monthly_savings", 0),
                        "recommendations": service_data.get("total_recommendations", 0),
                    }
                )

        # Sort by savings for better visualization
        chart_data.sort(key=lambda x: x["savings"], reverse=True)

        return f"""<script>
        let currentFilter = null;
        const chartData = {chart_data};
        
        // Dark Mode Functions
        function toggleTheme() {{
            const html = document.documentElement;
            const currentTheme = html.getAttribute('data-theme');
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            
            html.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeToggle(newTheme);
            
            // Reinitialize charts with new colors
            setTimeout(() => {{
                initializeCharts();
            }}, 100);
        }}

        function updateThemeToggle(theme) {{
            const icon = document.getElementById('theme-icon');
            const text = document.getElementById('theme-text');
            
            if (theme === 'dark') {{
                icon.textContent = '☀️';
                text.textContent = 'Light';
            }} else {{
                icon.textContent = '🌙';
                text.textContent = 'Dark';
            }}
        }}

        function initializeTheme() {{
            const savedTheme = localStorage.getItem('theme') || 'light';
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeToggle(savedTheme);
        }}
        
        function showTab(tabId) {{
            // Hide all tab contents
            const contents = document.querySelectorAll('.tab-content');
            contents.forEach(content => content.classList.remove('active'));
            
            // Remove active class from all buttons
            const buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(button => button.classList.remove('active'));
            
            // Show selected tab content
            document.getElementById(tabId).classList.add('active');
            
            // Add active class to clicked button
            event.target.classList.add('active');
            
            // Clear filter when switching to executive summary
            if (tabId === 'executive-summary') {{
                currentFilter = null;
                updateFilterIndicators();
            }}
        }}
        
        function filterByService(serviceKey) {{
            currentFilter = serviceKey;
            updateFilterIndicators();
            
            // Show the specific service tab
            showTab(serviceKey);
        }}
        
        function updateFilterIndicators() {{
            const buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(button => {{
                if (currentFilter && button.onclick.toString().includes(currentFilter)) {{
                    button.style.background = 'var(--primary-light)';
                }} else {{
                    button.style.background = '';
                }}
            }});
        }}
        
        let pieChart = null;
        let barChart = null;
        
        function initializeCharts() {{
            if (chartData.length === 0) return;
            
            const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
            const borderColor = isDark ? '#1e1e1e' : '#fff';
            const textColor = isDark ? '#ffffff' : '#212121';
            const gridColor = isDark ? '#333333' : '#e0e0e0';
            
            // AWS Theme Colors
            const awsColors = [
                '#42a5f5', '#64b5f6', '#ffb74d', '#66bb6a', '#ef5350',
                '#ab47bc', '#26c6da', '#9ccc65', '#ffca28', '#8d6e63'
            ];
            
            // Destroy existing charts
            if (pieChart) {{
                pieChart.destroy();
                pieChart = null;
            }}
            if (barChart) {{
                barChart.destroy();
                barChart = null;
            }}
            
            // Pie Chart
            const pieCtx = document.getElementById('savingsPieChart');
            if (pieCtx) {{
                pieChart = new Chart(pieCtx, {{
                    type: 'pie',
                    data: {{
                        labels: chartData.map(d => d.service),
                        datasets: [{{
                            data: chartData.map(d => d.savings),
                            backgroundColor: awsColors.slice(0, chartData.length),
                            borderWidth: 2,
                            borderColor: borderColor
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                position: 'bottom',
                                labels: {{
                                    padding: 20,
                                    usePointStyle: true,
                                    color: textColor
                                }}
                            }},
                            tooltip: {{
                                titleColor: textColor,
                                bodyColor: textColor,
                                backgroundColor: isDark ? '#333333' : '#ffffff',
                                borderColor: isDark ? '#555555' : '#cccccc',
                                borderWidth: 1,
                                callbacks: {{
                                    label: function(context) {{
                                        const value = context.parsed;
                                        const total = context.dataset.data.reduce((a, b) => a + b, 0);
                                        const percentage = ((value / total) * 100).toFixed(1);
                                        return context.label + ': $' + value.toFixed(2) + ' (' + percentage + '%)';
                                    }}
                                }}
                            }}
                        }},
                        onClick: function(event, elements) {{
                            if (elements.length > 0) {{
                                const index = elements[0].index;
                                const serviceKey = chartData[index].service_key;
                                filterByService(serviceKey);
                            }}
                        }}
                    }}
                }});
            }}
            
            // Bar Chart
            const barCtx = document.getElementById('savingsBarChart');
            if (barCtx) {{
                barChart = new Chart(barCtx, {{
                    type: 'bar',
                    data: {{
                        labels: chartData.map(d => d.service),
                        datasets: [{{
                            label: 'Monthly Savings ($)',
                            data: chartData.map(d => d.savings),
                            backgroundColor: '#42a5f5',
                            borderColor: '#1976d2',
                            borderWidth: 1
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {{
                            legend: {{
                                display: false
                            }},
                            tooltip: {{
                                titleColor: textColor,
                                bodyColor: textColor,
                                backgroundColor: isDark ? '#333333' : '#ffffff',
                                borderColor: isDark ? '#555555' : '#cccccc',
                                borderWidth: 1,
                                callbacks: {{
                                    label: function(context) {{
                                        return 'Savings: $' + context.parsed.y.toFixed(2);
                                    }}
                                }}
                            }}
                        }},
                        scales: {{
                            y: {{
                                beginAtZero: true,
                                ticks: {{
                                    color: textColor,
                                    callback: function(value) {{
                                        return '$' + value.toFixed(0);
                                    }}
                                }},
                                grid: {{
                                    color: gridColor
                                }},
                                title: {{
                                    display: false
                                }}
                            }},
                            x: {{
                                ticks: {{
                                    color: textColor,
                                    maxRotation: 45
                                }},
                                grid: {{
                                    color: gridColor
                                }},
                                title: {{
                                    display: false
                                }}
                            }}
                        }},
                        onClick: function(event, elements) {{
                            if (elements.length > 0) {{
                                const index = elements[0].index;
                                const serviceKey = chartData[index].service_key;
                                filterByService(serviceKey);
                            }}
                        }}
                    }}
                }});
            }}
        }}
        
        // Initialize theme and charts when page loads
        document.addEventListener('DOMContentLoaded', function() {{
            initializeTheme();
            initializeCharts();
        }});
        </script>"""


def generate_html_report_from_json(json_file: str, output_file: str = None) -> str:
    """Generate HTML report from JSON scan results file"""
    with open(json_file, "r") as f:
        scan_results = json.load(f)

    generator = HTMLReportGenerator(scan_results)
    return generator.generate_html_report(output_file)


if __name__ == "__main__":
    """
    Command-line interface for generating HTML reports from existing JSON files.
    
    Usage:
        python3 html_report_generator.py <json_file> [output_file]
    
    Examples:
        python3 html_report_generator.py cost_optimization_scan_us-east-1_20260117_235644.json
        python3 html_report_generator.py scan_results.json custom_report.html
    """
    import sys

    if len(sys.argv) < 2:
        print("Usage: python3 html_report_generator.py <json_file> [output_file]")
        print("")
        print("Examples:")
        print("  python3 html_report_generator.py cost_optimization_scan_us-east-1_20260117_235644.json")
        print("  python3 html_report_generator.py scan_results.json custom_report.html")
        print("")
        print("The script will:")
        print("  1. Load the existing JSON scan results")
        print("  2. Generate a professional HTML report with all groupings")
        print("  3. Save as [profile]_[region].html or custom filename")
        sys.exit(1)

    json_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        # Load existing JSON scan results
        with open(json_file, "r") as f:
            scan_results = json.load(f)

        print(f"📊 Loading scan results from: {json_file}")

        # Generate HTML report
        generator = HTMLReportGenerator(scan_results)
        generated_file = generator.generate_html_report(output_file)

        print(f"✅ HTML report generated: {generated_file}")
        print(f"🌐 Open in browser to view interactive cost optimization recommendations")

    except FileNotFoundError:
        print(f"❌ Error: JSON file '{json_file}' not found")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"❌ Error: Invalid JSON format in '{json_file}'")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error generating report: {e}")
        sys.exit(1)
