using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;

record Sample(string Reference, string Rendered, int[] ValueUnits);

class Program
{
    static SyntaxToken[] Tokens(string source)
    {
        var tree = CSharpSyntaxTree.ParseText(source);
        var errors = tree.GetDiagnostics().Where(d => d.Severity == DiagnosticSeverity.Error);
        if (errors.Any())
            throw new InvalidDataException(string.Join("\n", errors));
        return tree.GetRoot().DescendantTokens().ToArray();
    }

    static void Check(Sample sample)
    {
        var before = Tokens(sample.Reference);
        var after = Tokens(sample.Rendered);
        if (before.Length != after.Length)
            throw new InvalidDataException($"Token count changed from {before.Length} to {after.Length}");
        string value = new string(sample.ValueUnits.Select(unit => (char)unit).ToArray());
        int matched = 0;
        foreach (var (a, b) in before.Zip(after))
        {
            bool slot = a.IsKind(SyntaxKind.StringLiteralToken) && a.ValueText.Contains("{name}");
            bool sameText = slot ? b.ValueText == a.ValueText.Replace("{name}", value) : a.Text == b.Text;
            if (!sameText || a.RawKind != b.RawKind || a.LeadingTrivia.ToFullString() != b.LeadingTrivia.ToFullString()
                || a.TrailingTrivia.ToFullString() != b.TrailingTrivia.ToFullString())
                throw new InvalidDataException($"Token changed at reference offset {a.SpanStart} ({a.Kind()})");
            if (slot) matched++;
        }
        if (matched == 0) throw new InvalidDataException("No reference literal contained a slot");
    }

    static void Main()
    {
        var samples = JsonSerializer.Deserialize<Sample[]>(File.ReadAllText("samples.json"));
        for (int i = 0; i < samples.Length; i++)
        {
            try { Check(samples[i]); }
            catch (Exception e) { throw new InvalidDataException($"Sample {i}: {samples[i].Reference}", e); }
        }
        Console.WriteLine(samples.Length);
    }
}
