using Azure.Storage.Blobs;
using Azure.Storage.Blobs.Models;
using Azure.Storage.Sas;
using Microsoft.AspNetCore.Mvc;
using PdfComparisonApp.Models;
using PdfComparisonApp.Services;
using System.Text.Json;
using Microsoft.Data.SqlClient;
using System.Data;
using System.Net.Http;

namespace PdfComparisonApp.Controllers
{
    public class ComparisonController : Controller
    {
        private readonly IConfiguration _configuration;
        private readonly IPdfApiService _pdfApiService;
        private readonly IHttpClientFactory _httpClientFactory;
        private readonly HttpClient _httpClient;
        private readonly string _connectionString;
        private readonly ILogger<ComparisonController> _logger;
        private readonly string _azureStorageConnectionString;
        private readonly string _blobContainerName;

        public ComparisonController(IHttpClientFactory httpClientFactory, IConfiguration configuration, IPdfApiService pdfApiService, ILogger<ComparisonController> logger)
        {
            _connectionString = configuration.GetConnectionString("DefaultConnection") ?? throw new InvalidOperationException("Connection string 'DefaultConnection' not found.");
            _pdfApiService = pdfApiService;
             _httpClientFactory = httpClientFactory;
            _httpClient = new HttpClient();
            _logger = logger;
            // Fetch Azure Blob credentials dynamically from appsettings.json
            _azureStorageConnectionString = configuration["AzureStorage:ConnectionString"]
                ?? throw new InvalidOperationException("AzureStorage:ConnectionString is missing in appsettings.json");
            _blobContainerName = configuration["AzureStorage:ContainerName"] ?? "agent0rps";

        }

        [HttpGet]
        public async Task<IActionResult> GetAllComparisonJson(int pdfIdV1, int pdfIdV2)
        {
            try
            {
                var pythonApiBaseUrl = "http://localhost:8000";

                var url =
                    $"{pythonApiBaseUrl}/api/pdf/comparison-json/{pdfIdV1}/{pdfIdV2}";

                var response = await _httpClient.GetAsync(url);

                if (!response.IsSuccessStatusCode)
                {
                    return NotFound();
                }

                var json = await response.Content.ReadAsStringAsync();

                return Content(json, "application/json");
            }
            catch (Exception ex)
            {
                return StatusCode(500, new
                {
                    message = "Failed to fetch comparison JSON.",
                    error = ex.Message
                });
            }
        }

        [HttpGet]
        public async Task<IActionResult> GetComparisonJson(
            int pdfIdV1,
            int pdfIdV2,
            int chapterNumber)
        {
            var pythonApiBaseUrl = "http://localhost:8000";

            var url =
                $"{pythonApiBaseUrl}/api/pdf/comparison-json/{pdfIdV1}/{pdfIdV2}/{chapterNumber}";

            var response = await _httpClient.GetAsync(url);

            if (!response.IsSuccessStatusCode)
                return NotFound();

            var json = await response.Content.ReadAsStringAsync();

            return Content(json, "application/json");
        }

        // GET: /Comparison/Index
        public IActionResult Index()
        {
            return View();
        }

        // ==========================================
        // DIRECT DOTNET SQL SERVER HISTORY LOADER
        // ==========================================
        [HttpGet]
        public async Task<IActionResult> History()
        {
            var historyList = new List<object>();
            ViewBag.ErrorMessage = null;

            try
            {
                using (var conn = new SqlConnection(_connectionString))
                {
                    await conn.OpenAsync();

                    // Query to fetch unique comparison pairs and group them with document details
                    string query = @"
                        SELECT 
                            c.pdf_id_v1 AS pdf_id_v1, 
                            c.pdf_id_v2 AS pdf_id_v2, 
                            p2.pdf_name AS v1_name, 
                            p1.pdf_version AS v1_version, 
                            p2.pdf_version AS v2_version,
                            COUNT(c.id) AS total_chapters_compared,
                            CONVERT(VARCHAR, MAX(c.comparison_date), 120) AS last_compared
                        FROM [chapter_comparisons] c
                        INNER JOIN [pdf_documents] p1 ON c.pdf_id_v1 = p1.id
                        INNER JOIN [pdf_documents] p2 ON c.pdf_id_v2 = p2.id
                        GROUP BY c.pdf_id_v1, c.pdf_id_v2, p2.pdf_name, p1.pdf_version, p2.pdf_version
                        ORDER BY MAX(c.comparison_date) DESC";

                    using (var cmd = new SqlCommand(query, conn))
                    using (var reader = await cmd.ExecuteReaderAsync())
                    {
                        while (await reader.ReadAsync())
                        {
                            historyList.Add(new
                            {
                                pdf_id_v1 = reader.GetInt32(0),
                                pdf_id_v2 = reader.GetInt32(1),
                                v1_name = reader.GetString(2),
                                v1_version = reader.GetString(3),
                                v2_version = reader.GetString(4),
                                total_chapters_compared = reader.GetInt32(5),
                                last_compared = reader.GetString(6)
                            });
                        }
                    }
                }

                // Match exactly the expected format in History.cshtml: JSON object containing a "history" array
                var finalJsonObj = new { history = historyList };
                ViewBag.History = JsonSerializer.SerializeToElement(finalJsonObj);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error reading comparison history directly from DB");
                ViewBag.ErrorMessage = "Database Connection Failed: " + ex.Message;
                ViewBag.History = null;
            }

            return View();
        }

        // ==========================================
        // DIRECT DOTNET HISTORY DETAIL LOADER
        // ==========================================
        [HttpGet]
        public async Task<IActionResult> HistoryDetail(int pdfIdV1, int pdfIdV2)
        {
            ViewBag.ErrorMessage = null;
            ViewBag.PdfUrlV1 = Url.Action("GetPdfStream", "Comparison", new { id = pdfIdV1 });
            ViewBag.PdfUrlV2 = Url.Action("GetPdfStream", "Comparison", new { id = pdfIdV2, isVersion2 = true });

            try
            {
                string v1Name = "Medicare EOC Document";
                string v1Ver = "Unknown";
                string v2Ver = "Unknown";
                var chaptersList = new List<object>();

                using (var conn = new SqlConnection(_connectionString))
                {
                    await conn.OpenAsync();

                    // 1. Fetch document metadata
                    string docQuery = @"
                        SELECT pdf_name, pdf_version FROM [pdf_documents] WHERE id = @Id1; 
                        SELECT pdf_version FROM [pdf_documents] WHERE id = @Id2;";
                    using (var cmd = new SqlCommand(docQuery, conn))
                    {
                        cmd.Parameters.AddWithValue("@Id1", pdfIdV1);
                        cmd.Parameters.AddWithValue("@Id2", pdfIdV2);

                        using (var reader = await cmd.ExecuteReaderAsync())
                        {
                            if (await reader.ReadAsync())
                            {
                                v1Name = reader.GetString(0);
                                v1Ver = reader.GetString(1);
                            }
                            if (await reader.NextResultAsync() && await reader.ReadAsync())
                            {
                                v2Ver = reader.GetString(0);
                            }
                        }
                    }

                    // 2. Fetch all chapter comparisons for this pair
                    string chaptersQuery = @"
                        SELECT 
                            chapter_number,
                            v1_chapter_title,
                            v2_chapter_title,
                            llm_response,
                            CONVERT(VARCHAR, comparison_date, 120) AS comparison_date
                        FROM [chapter_comparisons]
                        WHERE pdf_id_v1 = @PdfIdV1 AND pdf_id_v2 = @PdfIdV2
                        ORDER BY chapter_number ASC";

                    using (var cmd = new SqlCommand(chaptersQuery, conn))
                    {
                        cmd.Parameters.AddWithValue("@PdfIdV1", pdfIdV1);
                        cmd.Parameters.AddWithValue("@PdfIdV2", pdfIdV2);

                        using (var reader = await cmd.ExecuteReaderAsync())
                        {
                            while (await reader.ReadAsync())
                            {
                                chaptersList.Add(new
                                {
                                    chapter_number = reader.GetInt32(0),
                                    v1_title = reader.IsDBNull(1) ? "" : reader.GetString(1),
                                    v2_title = reader.IsDBNull(2) ? "" : reader.GetString(2),
                                    llm_response = reader.IsDBNull(3) ? "" : reader.GetString(3),
                                    comparison_date = reader.IsDBNull(4) ? "" : reader.GetString(4)
                                });
                            }
                        }
                    }
                }

                // Match exactly the expected format in HistoryDetail.cshtml:
                var finalDetailObj = new
                {
                    v1_name = v1Name,
                    v1_version = v1Ver,
                    v2_version = v2Ver,
                    total_chapters = chaptersList.Count,
                    chapters = chaptersList
                };

                ViewBag.Detail = JsonSerializer.SerializeToElement(finalDetailObj);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error reading comparison details directly from DB");
                ViewBag.ErrorMessage = "Failed to load details: " + ex.Message;
                ViewBag.Detail = null;
            }

            return View();
        }

        [HttpGet]
        public async Task<IActionResult> GetHistory()
        {
            var historyList = new List<object>();

            try
            {
                using (var conn = new SqlConnection(_connectionString))
                {
                    await conn.OpenAsync();

                    string query = @"
                SELECT 
                    c.pdf_id_v1,
                    c.pdf_id_v2,
                    p1.pdf_name AS v1_name,
                    p1.pdf_version AS v1_version,
                    p2.pdf_name AS v2_name,
                    p2.pdf_version AS v2_version,
                    COUNT(DISTINCT c.chapter_number) AS total_chapters_compared,
                    CONVERT(VARCHAR, MAX(c.comparison_date), 120) AS last_compared
                FROM [chapter_comparisons] c
                INNER JOIN [pdf_documents] p1 
                    ON c.pdf_id_v1 = p1.id
                INNER JOIN [pdf_documents] p2 
                    ON c.pdf_id_v2 = p2.id
                GROUP BY 
                    c.pdf_id_v1,
                    c.pdf_id_v2,
                    p1.pdf_name,
                    p1.pdf_version,
                    p2.pdf_name,
                    p2.pdf_version
                ORDER BY MAX(c.comparison_date) DESC";

                    using (var cmd = new SqlCommand(query, conn))
                    using (var reader = await cmd.ExecuteReaderAsync())
                    {
                        while (await reader.ReadAsync())
                        {
                            historyList.Add(new
                            {
                                pdf_id_v1 = reader.GetInt32(0),
                                pdf_id_v2 = reader.GetInt32(1),

                                v1_name = reader.IsDBNull(2)
                                    ? ""
                                    : reader.GetString(2),

                                v1_version = reader.IsDBNull(3)
                                    ? ""
                                    : reader.GetString(3),

                                v2_name = reader.IsDBNull(4)
                                    ? ""
                                    : reader.GetString(4),

                                v2_version = reader.IsDBNull(5)
                                    ? ""
                                    : reader.GetString(5),

                                total_chapters_compared = reader.GetInt32(6),

                                last_compared = reader.IsDBNull(7)
                                    ? ""
                                    : reader.GetString(7)
                            });
                        }
                    }
                }

                return Json(new
                {
                    success = true,
                    history = historyList
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(
                    ex,
                    "Error reading comparison history"
                );

                return StatusCode(500, new
                {
                    success = false,
                    message = "Failed to load comparison history.",
                    error = ex.Message
                });
            }
        }

        [HttpGet]
        public async Task<IActionResult> GetSessionMetadata(int pdfIdV1, int pdfIdV2)
        {
            try
            {
                string v1Name = "";
                string v1Version = "";
                string v2Name = "";
                string v2Version = "";

                var chaptersList = new List<object>();

                using (var conn = new SqlConnection(_connectionString))
                {
                    await conn.OpenAsync();

                    // 1. PDF metadata
                    string docQuery = @"
                SELECT pdf_name, pdf_version
                FROM [pdf_documents]
                WHERE id = @PdfIdV1;

                SELECT pdf_name, pdf_version
                FROM [pdf_documents]
                WHERE id = @PdfIdV2;
            ";

                    using (var cmd = new SqlCommand(docQuery, conn))
                    {
                        cmd.Parameters.AddWithValue("@PdfIdV1", pdfIdV1);
                        cmd.Parameters.AddWithValue("@PdfIdV2", pdfIdV2);

                        using (var reader = await cmd.ExecuteReaderAsync())
                        {
                            if (await reader.ReadAsync())
                            {
                                v1Name = reader.IsDBNull(0) ? "" : reader.GetString(0);
                                v1Version = reader.IsDBNull(1) ? "" : reader.GetString(1);
                            }

                            if (await reader.NextResultAsync() &&
                                await reader.ReadAsync())
                            {
                                v2Name = reader.IsDBNull(0) ? "" : reader.GetString(0);
                                v2Version = reader.IsDBNull(1) ? "" : reader.GetString(1);
                            }
                        }
                    }

                    // 2. Chapter comparison data
                    string chaptersQuery = @"
                SELECT
                    chapter_number,
                    v1_chapter_title,
                    v2_chapter_title,
                    llm_response,
                    CONVERT(VARCHAR, comparison_date, 120)
                FROM [chapter_comparisons]
                WHERE pdf_id_v1 = @PdfIdV1
                  AND pdf_id_v2 = @PdfIdV2
                ORDER BY chapter_number ASC;
            ";

                    using (var cmd = new SqlCommand(chaptersQuery, conn))
                    {
                        cmd.Parameters.AddWithValue("@PdfIdV1", pdfIdV1);
                        cmd.Parameters.AddWithValue("@PdfIdV2", pdfIdV2);

                        using (var reader = await cmd.ExecuteReaderAsync())
                        {
                            while (await reader.ReadAsync())
                            {
                                chaptersList.Add(new
                                {
                                    chapter_number = reader.GetInt32(0),

                                    v1_title = reader.IsDBNull(1)
                                        ? ""
                                        : reader.GetString(1),

                                    v2_title = reader.IsDBNull(2)
                                        ? ""
                                        : reader.GetString(2),

                                    llm_response = reader.IsDBNull(3)
                                        ? ""
                                        : reader.GetString(3),

                                    comparison_date = reader.IsDBNull(4)
                                        ? ""
                                        : reader.GetString(4)
                                });
                            }
                        }
                    }
                }

                return Json(new
                {
                    success = true,

                    v1_name = v1Name,
                    v1_version = v1Version,

                    v2_name = v2Name,
                    v2_version = v2Version,

                    total_chapters = chaptersList.Count,

                    chapters = chaptersList
                });
            }
            catch (Exception ex)
            {
                _logger.LogError(
                    ex,
                    "Error loading session metadata for PDF {PdfIdV1} vs {PdfIdV2}",
                    pdfIdV1,
                    pdfIdV2
                );

                return StatusCode(500, new
                {
                    success = false,
                    message = "Failed to load session metadata.",
                    error = ex.Message
                });
            }
        }
        // =========================================================================
        // 3. PDF STREAM PROXY (Streams PDF file for side-by-side viewer)
        // =========================================================================

        // ============================================================
        // PROXY STREAMING ENDPOINT (ELIMINATES CORS ERRORS ENTIRELY)
        // ============================================================


        [HttpGet("Comparison/GetPdfStream/{id}")]

        public async Task<IActionResult> GetPdfStream(int id, [FromQuery] bool isVersion2 = false)
        {
            try
            {
                var containerClient = new BlobContainerClient(
                    _azureStorageConnectionString,
                    _blobContainerName);

                if (!await containerClient.ExistsAsync())
                {
                    return NotFound(
                        $"Azure Blob Container '{_blobContainerName}' was not found.");
                }

                // Get exact blob name using PDF ID
                string blobName = null;

                using (var connection = new SqlConnection(_connectionString))
                {
                    await connection.OpenAsync();

                    using var command = new SqlCommand(@"
                        SELECT blob_name
                        FROM pdf_documents
                        WHERE id = @id
                    ", connection);

                    command.Parameters.AddWithValue("@id", id);

                    var result = await command.ExecuteScalarAsync();

                    if (result != null && result != DBNull.Value)
                    {
                        blobName = result.ToString();
                    }
                }

                if (string.IsNullOrWhiteSpace(blobName))
                {
                    return NotFound(
                        $"No blob mapping found for PDF ID {id}.");
                }

                // Get EXACT blob
                var blobClient = containerClient.GetBlobClient(blobName);

                if (!await blobClient.ExistsAsync())
                {
                    return NotFound(
                        $"Blob '{blobName}' was not found in Azure Storage.");
                }

                Console.WriteLine(
                    $"--> [System] PDF ID {id} mapped to blob: '{blobName}'");

                var blobStream = await blobClient.OpenReadAsync();

                return File(blobStream, "application/pdf");
            }

            catch (Exception ex)
            {
                Console.WriteLine(
                    $"[ERROR] GetPdfStream({id}) failed: {ex}");

                return StatusCode(
                    500,
                    $"Azure Blob Streaming Error: {ex.Message}");
            }
        }

        // POST: /Comparison/ProcessPdfs (AJAX Endpoint)
        [HttpPost]
        public async Task<IActionResult> ProcessPdfs([FromForm] UploadPdfViewModel model)
        {
            try
            {
                if (!ModelState.IsValid)
                {
                    return BadRequest(new { success = false, message = "Please select both PDF files." });
                }

                var result = await _pdfApiService.ProcessPdfsAsync(model);
                return Json(result);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error processing PDFs");
                return StatusCode(500, new { success = false, message = ex.Message });
            }
        }

        // POST: /Comparison/CompareAll (AJAX Endpoint)
        [HttpPost]
        public async Task<IActionResult> CompareAll([FromBody] CompareAllRequestModel request)
        {
            try
            {
                var result = await _pdfApiService.CompareAllChaptersAsync(request.PdfIdV1, request.PdfIdV2);
                _logger.LogInformation(
                    "CompareAll result: {@Result}",
                    result
                );

                return Json(result);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error comparing all chapters");
                return StatusCode(500, new { success = false, message = ex.Message });
            }
        }

        // POST: /Comparison/CompareSingle (AJAX Endpoint)
        [HttpPost]
        public async Task<IActionResult> CompareSingle([FromBody] CompareSingleRequestModel request)
        {
            try
            {
                var result = await _pdfApiService.CompareSingleChapterAsync(request.PdfIdV1, request.PdfIdV2, request.ChapterNumber);
                return Json(new { success = true, data = result });
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error comparing single chapter");
                return StatusCode(500, new { success = false, message = ex.Message });
            }
        }

        //public async Task<IActionResult> History()
        //{
        //    try
        //    {
        //        Console.WriteLine("--> [DEBUG] Calling Python History API...");
        //        var data = await _pdfApiService.GetHistoryAsync();
        //        Console.WriteLine("--> [DEBUG] History API Success! Data received. ", data);
        //        ViewBag.History = data;
        //        return View();
        //    }
        //    catch (HttpRequestException httpEx)
        //    {
        //        Console.WriteLine($"--> [ERROR] Python API is unreachable: {httpEx.Message}");
        //        ViewBag.ErrorMessage = "Python Backend server ! Please make sure that Python app is ruuning (port 8000).";
        //        return View();
        //    }
        //    catch (Exception ex)
        //    {
        //        Console.WriteLine($"--> [ERROR] History Exception: {ex.Message}");
        //        Console.WriteLine(ex.StackTrace);
        //        TempData["Error"] = "Failed to load history: " + ex.Message;
        //        return View();
        //    }
        //}

        //// ============================================================
        //// HISTORY DETAIL ACTION
        //// ============================================================
        //// ============================================================
        //// HISTORY DETAIL ACTION
        //// ============================================================
        //[HttpGet]
        //public async Task<IActionResult> HistoryDetail(int pdfIdV1, int pdfIdV2)
        //{
        //    try
        //    {
        //        // 1. Fetch details from Python API
        //        var detailData = await _pdfApiService.GetComparisonDetailAsync(pdfIdV1, pdfIdV2);
        //        ViewBag.Detail = detailData;

        //        // 2. Set default blob names matching actual files in Azure Storage
        //        string v1Name = "Health Insurance Policy V1.pdf";
        //        string v2Name = "Health Insurance Policy V2.pdf";

        //        // Check if API returned dynamic names
        //        if (detailData.TryGetProperty("v1_name", out JsonElement v1Prop) && v1Prop.ValueKind == JsonValueKind.String)
        //        {
        //            string dynamicV1 = v1Prop.GetString();
        //            if (!string.IsNullOrEmpty(dynamicV1) && !dynamicV1.Contains("#"))
        //            {
        //                v1Name = dynamicV1.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) ? dynamicV1 : dynamicV1 + ".pdf";
        //            }
        //        }

        //        if (detailData.TryGetProperty("v2_name", out JsonElement v2Prop) && v2Prop.ValueKind == JsonValueKind.String)
        //        {
        //            string dynamicV2 = v2Prop.GetString();
        //            if (!string.IsNullOrEmpty(dynamicV2) && !dynamicV2.Contains("#"))
        //            {
        //                v2Name = dynamicV2.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase) ? dynamicV2 : dynamicV2 + ".pdf";
        //            }
        //        }

        //        // 3. Generate stream URLs passing version indicators
        //        ViewBag.PdfUrlV1 = Url.Action("StreamPdf", "Comparison", new { blobName = v1Name, isVersion2 = false });
        //        ViewBag.PdfUrlV2 = Url.Action("StreamPdf", "Comparison", new { blobName = v2Name, isVersion2 = true });

        //        return View();
        //    }
        //    catch (Exception ex)
        //    {
        //        TempData["Error"] = $"Unable to load details: {ex.Message}";
        //        return RedirectToAction("History");
        //    }
        //}

        // ============================================================
        // AZURE BLOB PROXY STREAMER (EXACT MATCH + SMART FALLBACK)
        // ============================================================
        [HttpGet]
        public async Task<IActionResult> StreamPdf(string blobName, bool isVersion2 = false)
        {
            try
            {
                var containerClient = new BlobContainerClient(_azureStorageConnectionString, _blobContainerName);

                if (!await containerClient.ExistsAsync())
                {
                    return StatusCode(500, $"Container '{_blobContainerName}' was not found in Azure Storage.");
                }

                // Fetch all actual blob file names from container
                var allBlobs = new List<string>();
                await foreach (BlobItem blob in containerClient.GetBlobsAsync())
                {
                    allBlobs.Add(blob.Name);
                }

                if (!allBlobs.Any())
                {
                    return NotFound($"No PDF files exist inside Azure container '{_blobContainerName}'.");
                }

                string matchedBlobName = null;

                // Priority 1: Exact Match (e.g. "Health Insurance Policy V1.pdf")
                if (!string.IsNullOrEmpty(blobName))
                {
                    matchedBlobName = allBlobs.FirstOrDefault(b =>
                        b.Equals(blobName, StringComparison.OrdinalIgnoreCase));
                }

                // Priority 2: Partial Keyword Match ("Health", "Policy", "Insurance")
                if (string.IsNullOrEmpty(matchedBlobName) && !string.IsNullOrEmpty(blobName))
                {
                    string cleanSearch = blobName.Replace(".pdf", "", StringComparison.OrdinalIgnoreCase).Trim();
                    matchedBlobName = allBlobs.FirstOrDefault(b =>
                        b.Contains(cleanSearch, StringComparison.OrdinalIgnoreCase));
                }

                // Priority 3: Version Marker Match ("V1" vs "V2")
                if (string.IsNullOrEmpty(matchedBlobName))
                {
                    string targetTag = isVersion2 ? "V2" : "V1";
                    matchedBlobName = allBlobs.FirstOrDefault(b =>
                        b.ToUpper().Contains(targetTag));
                }

                // Priority 4: Positional Fallback (First PDF for V1, Second PDF for V2)
                if (string.IsNullOrEmpty(matchedBlobName))
                {
                    var pdfs = allBlobs.Where(b => b.EndsWith(".pdf", StringComparison.OrdinalIgnoreCase)).ToList();
                    if (pdfs.Count >= 2)
                    {
                        matchedBlobName = isVersion2 ? pdfs[1] : pdfs[0];
                    }
                    else if (pdfs.Any())
                    {
                        matchedBlobName = pdfs[0];
                    }
                }

                if (string.IsNullOrEmpty(matchedBlobName))
                {
                    return NotFound($"Could not locate file '{blobName}' in Azure Storage.");
                }

                Console.WriteLine($"--> [System] Successfully streaming Azure Blob: '{matchedBlobName}'");

                var blobClient = containerClient.GetBlobClient(matchedBlobName);
                var blobStream = await blobClient.OpenReadAsync();

                return File(blobStream, "application/pdf");
            }
            catch (Exception ex)
            {
                return StatusCode(500, $"Azure Blob Streaming Error: {ex.Message}");
            }
        }


        // ... inside ComparisonController ...

        private string GetProtectedBlobUrl(string blobName)
        {
            try
            {
                string connectionString = _azureStorageConnectionString;
                string containerName = _blobContainerName;

                var blobServiceClient = new BlobServiceClient(connectionString);
                var containerClient = blobServiceClient.GetBlobContainerClient(containerName);
                var blobClient = containerClient.GetBlobClient(blobName);

                // Generate a SAS token that is valid for 1 hour
                BlobSasBuilder sasBuilder = new BlobSasBuilder()
                {
                    BlobContainerName = containerName,
                    BlobName = blobName,
                    Resource = "b",
                    ExpiresOn = DateTimeOffset.UtcNow.AddHours(1)
                };
                sasBuilder.SetPermissions(BlobSasPermissions.Read);

                Uri sasUri = blobClient.GenerateSasUri(sasBuilder);
                return sasUri.ToString();
            }
            catch (Exception)
            {
                // Fallback or log error
                return "";
            }
        }
    }



    public class CompareAllRequestModel
    {
        public int PdfIdV1 { get; set; }
        public int PdfIdV2 { get; set; }
    }

    public class CompareSingleRequestModel
    {
        public int PdfIdV1 { get; set; }
        public int PdfIdV2 { get; set; }
        public int ChapterNumber { get; set; }
    }
}
