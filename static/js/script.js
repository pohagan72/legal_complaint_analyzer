// script.js
document.addEventListener('DOMContentLoaded', () => {
    const analyzeForm = document.getElementById('analyzeForm');
    const fileInput = document.getElementById('file');
    const resultsContainer = document.getElementById('resultsContainer');
    const loadingOverlay = document.getElementById('loadingOverlay');
    const flashesContainer = document.getElementById('flashesContainer');
    
    // New: Drag and Drop elements
    const fileDropArea = document.getElementById('fileDropArea');
    const fileNameDisplay = document.getElementById('fileNameDisplay');
    const fileDropMessage = document.querySelector('.file-drop-message');
    const fileDropIcon = document.querySelector('.file-drop-icon');

    // --- Drag and Drop Functionality ---
    if (fileDropArea) {
        // Prevent default browser behaviors
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            fileDropArea.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            }, false);
        });

        // Add visual feedback on drag over
        ['dragenter', 'dragover'].forEach(eventName => {
            fileDropArea.addEventListener(eventName, () => {
                fileDropArea.classList.add('drag-over');
            }, false);
        });

        // Remove visual feedback on drag leave
        ['dragleave', 'drop'].forEach(eventName => {
            fileDropArea.addEventListener(eventName, () => {
                fileDropArea.classList.remove('drag-over');
            }, false);
        });

        // Handle the dropped file
        fileDropArea.addEventListener('drop', (e) => {
            const dt = e.dataTransfer;
            const files = dt.files;

            if (files.length > 0) {
                fileInput.files = files; // Assign dropped file to the hidden input
                updateFileDisplay(files[0]);
            }
        }, false);
    }
    
    // Update display when file is selected via click
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            updateFileDisplay(fileInput.files[0]);
        }
    });

    // Helper function to update the drop area's UI
    function updateFileDisplay(file) {
        if (file) {
            fileNameDisplay.textContent = file.name;
            if(fileDropMessage) fileDropMessage.style.display = 'none';
            if(fileDropIcon) fileDropIcon.style.display = 'none';
        }
    }

    // Function to show the loading overlay
    function showLoading() {
        loadingOverlay.style.display = 'flex';
        analyzeForm.querySelector('button[type="submit"]').disabled = true;
    }

    // Function to hide the loading overlay
    function hideLoading() {
        loadingOverlay.style.display = 'none';
        analyzeForm.querySelector('button[type="submit"]').disabled = false;
    }

    // Function to display temporary flash messages
    function displayFlashMessage(message, type) {
        const messageDiv = document.createElement('li');
        messageDiv.className = type; // 'success', 'info', 'warning', 'danger'
        messageDiv.textContent = message;
        flashesContainer.innerHTML = ''; // Clear previous messages
        flashesContainer.appendChild(messageDiv);
        flashesContainer.style.display = 'block';

        // Hide after a few seconds
        setTimeout(() => {
            flashesContainer.style.display = 'none';
            flashesContainer.innerHTML = '';
        }, 8000); // Hide after 8 seconds
    }

    // Event listener for form submission
    analyzeForm.addEventListener('submit', async (event) => {
        event.preventDefault(); // Prevent default form submission (page reload)

        // Clear previous results, download button, and messages
        resultsContainer.innerHTML = '';
        resultsContainer.style.display = 'none';
        flashesContainer.innerHTML = '';
        flashesContainer.style.display = 'none';

        if (!fileInput.files.length) {
            displayFlashMessage("Please select a file to analyze.", "warning");
            return;
        }

        // --- NEW: Client-side file type validation ---
        const allowedExtensions = ['.pdf', '.docx'];
        const fileName = fileInput.files[0].name;
        const fileExtension = fileName.substring(fileName.lastIndexOf('.')).toLowerCase();
        
        if (!allowedExtensions.includes(fileExtension)) {
            displayFlashMessage("Invalid file type. Please upload a PDF or DOCX document.", "danger");
            return; // Stop the submission
        }
        // --- END of new validation ---

        const formData = new FormData(analyzeForm);

        showLoading(); // Show loading indicator

        try {
            const response = await fetch('/analyze', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (data.status === 'success') {
                resultsContainer.innerHTML = '<h3>Analysis Results:</h3>'; // Start with heading
                resultsContainer.style.display = 'block';

                // BEFORE displaying results, check for and display the Excel download button
                if (data.excel_filename) {
                    displayDownloadButton(data.excel_filename);
                }

                if (data.results && data.results.length > 0) {
                    displayAnalysisResults(data.results); // Renamed function for clarity
                } else {
                    displayFlashMessage(data.message || "Analysis completed, but no allegations were identified.", "info");
                }
            } else if (data.status === 'info') {
                displayFlashMessage(data.message || "Analysis completed, but no allegations were identified.", "info");
            } else { // status === 'error'
                displayFlashMessage(data.message || "An unknown error occurred during analysis.", "danger");
            }

        } catch (error) {
            console.error('Fetch error:', error);
            displayFlashMessage(`Network or server error: ${error.message}. Please try again.`, "danger");
        } finally {
            hideLoading(); // Hide loading indicator regardless of success or failure
        }
    });

    // Function to display the Excel download button (moved to the top of resultsContainer)
    function displayDownloadButton(filename) {
        const downloadDiv = document.createElement('div');
        downloadDiv.className = 'download-section';
        downloadDiv.innerHTML = `
            <a href="/download_report/${filename}" class="download-button" download="${filename}">
                <i class="fas fa-file-excel"></i> Download Excel Report
            </a>
            <p class="download-tip">Click to download the full analysis report.</p>
        `;
        resultsContainer.appendChild(downloadDiv);
    }

    // Renamed function to display analysis results (excluding the download button logic)
    function displayAnalysisResults(results) {
        // Group results by Product_Name
        const groupedResults = results.reduce((acc, current) => {
            // Use current.Product_Name, ensuring it's a string or defaults
            const productName = current.Product_Name && typeof current.Product_Name === 'string'
                                ? current.Product_Name.trim()
                                : "No Product Mentioned";

            if (!acc[productName]) {
                acc[productName] = [];
            }
            acc[productName].push(current);
            return acc;
        }, {});

        // Sort product names alphabetically, pushing "No Product Mentioned" and "ERROR" to end
        const sortedProductNames = Object.keys(groupedResults).sort((a, b) => {
            if (a === "No Product Mentioned") return 1;
            if (b === "No Product Mentioned") return -1;
            if (a === "ERROR") return 1;
            if (b === "ERROR") return -1;
            return a.localeCompare(b);
        });

        sortedProductNames.forEach(productName => {
            const productGroupDiv = document.createElement('div');
            productGroupDiv.className = 'product-group-card';

            // Add an arrow icon for expand/collapse
            const headerHtml = `
                <h4>
                    ${productName}
                    <i class="fas fa-chevron-down toggle-icon"></i>
                </h4>
            `;
            productGroupDiv.innerHTML = headerHtml;

            const groupContent = document.createElement('div');
            groupContent.className = 'group-content hidden'; // Initially hidden

            groupedResults[productName].forEach(result => {
                const resultItem = document.createElement('div');
                resultItem.className = 'result-item';

                // Handle Pin_Cite_Page and Pin_Cite_Paragraph for display
                let pinCiteDisplay = 'N/A';
                if (result.Pin_Cite_Page && result.Pin_Cite_Page !== 'N/A') {
                    if (String(result.Pin_Cite_Page).startsWith('DOCX_Chunk_')) {
                        // For DOCX chunks, display as is
                        pinCiteDisplay = result.Pin_Cite_Page;
                    } else {
                        // Assume it's a PDF page number or a number-like string
                        pinCiteDisplay = `p. ${result.Pin_Cite_Page}`;
                    }
                    
                    if (result.Pin_Cite_Paragraph && result.Pin_Cite_Paragraph !== 'N/A') {
                        pinCiteDisplay += `, ¶${result.Pin_Cite_Paragraph}`;
                    }
                }

                resultItem.innerHTML = `
                    <p><strong>Allegation Category:</strong> ${result.Allegation_Category || 'N/A'}</p>
                    <p><strong>Specific Allegation Summary:</strong> ${result.Specific_Allegation_Summary || 'N/A'}</p>
                    <p><strong>Involved Defendants/Co-Conspirators:</strong> ${result.Involved_Defendants_CoConspirators || 'N/A'}</p>
                    <p><strong>Pin Cite:</strong> ${pinCiteDisplay}</p>
                `;
                groupContent.appendChild(resultItem);
            });
            productGroupDiv.appendChild(groupContent);
            resultsContainer.appendChild(productGroupDiv);

            // Add click listener to the header (h4) of the product group
            productGroupDiv.querySelector('h4').addEventListener('click', (e) => {
                const content = productGroupDiv.querySelector('.group-content');
                const icon = productGroupDiv.querySelector('.toggle-icon');
                if (content && icon) {
                    content.classList.toggle('hidden');
                    icon.classList.toggle('fa-chevron-down');
                    icon.classList.toggle('fa-chevron-up');
                    productGroupDiv.classList.toggle('expanded'); // Add a class for styling expanded state
                }
            });
        });
    }
});