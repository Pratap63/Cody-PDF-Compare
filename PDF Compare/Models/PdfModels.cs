using System.ComponentModel.DataAnnotations;
using Microsoft.AspNetCore.Http;
using System.Text.Json.Serialization;

namespace PdfComparisonApp.Models
{
    // Upload Form Model
    public class UploadPdfViewModel
    {
        [Required(ErrorMessage = "Please select Version 1 PDF")]
        public IFormFile PdfV1 { get; set; }

        [Required(ErrorMessage = "Please select Version 2 PDF")]
        public IFormFile PdfV2 { get; set; }

        public string DocName { get; set; } = "EOC Medicare Advantage";
        public string VersionV1 { get; set; } = "2027";
        public string VersionV2 { get; set; } = "2028";
    }

    // Python API Process Response
    public class ProcessPdfApiResponse
    {
        [JsonPropertyName("success")]
        public bool Success { get; set; }

        [JsonPropertyName("message")]
        public string Message { get; set; }

        [JsonPropertyName("pdf_id_v1")]
        public int PdfIdV1 { get; set; }

        [JsonPropertyName("pdf_id_v2")]
        public int PdfIdV2 { get; set; }

        [JsonPropertyName("chapters_v1_count")]
        public int ChaptersV1Count { get; set; }

        [JsonPropertyName("chapters_v2_count")]
        public int ChaptersV2Count { get; set; }
    }

    // Single Comparison Result
    public class ChapterComparisonDto
    {
        [JsonPropertyName("chapter_number")]
        public int ChapterNumber { get; set; }

        [JsonPropertyName("v1_title")]
        public string V1Title { get; set; }

        [JsonPropertyName("v2_title")]
        public string V2Title { get; set; }

        [JsonPropertyName("llm_response")]
        public string LlmResponse { get; set; }

        [JsonPropertyName("summary")]
        public string Summary { get; set; }

        [JsonPropertyName("discrepancies")]
        public List<DiscrepancyDto> Discrepancies { get; set; } = new();
    }

    public class DiscrepancyDto
    {
        [JsonPropertyName("topic")]
        public string Topic { get; set; }

        [JsonPropertyName("change_type")]
        public string ChangeType { get; set; }

        [JsonPropertyName("v1_text")]
        public string V1Text { get; set; }

        [JsonPropertyName("v2_text")]
        public string V2Text { get; set; }

        [JsonPropertyName("v1_location")]
        public object V1Location { get; set; }

        [JsonPropertyName("v2_location")]
        public object V2Location { get; set; }
    }

    // Compare All Response
    public class CompareAllApiResponse
    {
        [JsonPropertyName("success")]
        public bool Success { get; set; }

        [JsonPropertyName("total_compared")]
        public int TotalCompared { get; set; }

        [JsonPropertyName("results")]
        public List<ChapterComparisonDto> Results { get; set; } = new();
    }
}