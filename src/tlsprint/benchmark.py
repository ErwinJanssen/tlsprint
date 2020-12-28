import collections
import multiprocessing
import operator
import pathlib
import time

import pandas
import seaborn
import tqdm
from matplotlib import pyplot
from statsmodels.stats import weightstats

from . import util
from .identify import INPUT_SELECTORS
from .identify import MODEL_WEIGHTS
from .identify import identify
from .trees import trees


def count_inputs(messages):
    return len(messages) // 2


def count_resets(messages):
    return messages.count("RESET")


PATH_VALUES = {
    "inputs": count_inputs,
    "resets": count_resets,
}


def benchmark_model(tree, model, selector, weight_function):
    start_time = time.perf_counter()
    candidate_models, path = identify(
        tree, model, benchmark=True, selector=selector, weight_function=weight_function,
    )
    end_time = time.perf_counter()

    # The benchmark should always return one model: the candidate model. Any
    # other scenario must be aborted.
    if candidate_models != {model}:
        raise RuntimeError(
            "Unexpected benchmark model candidates."
            f"Expected {{model}}, but got {candidate_models}"
        )

    results = {name: value(path) for name, value in PATH_VALUES.items()}
    results["time"] = end_time - start_time

    return results


def benchmark(info):
    """Return the inputs and outputs used to identify each model in the
    tree."""
    tree = info["Tree"]
    model = info["Model"]
    selector = INPUT_SELECTORS[info["Input selector"]]
    weight_function = MODEL_WEIGHTS[info["Weight function"]]
    iterations = info["Iterations"]

    results = []
    for _ in range(iterations):
        results.append(benchmark_model(tree, model, selector, weight_function))

    # Copy most, but not all values from the info object
    benchmark_results = {
        key: value for key, value in info.items() if key not in ("Tree", "Iterations")
    }

    # Add the results and return it
    benchmark_results["Results"] = results

    return benchmark_results


def generate_benchmark_inputs(iterations):
    """Generate benchmarks as a Pandas DataFrame, where each row is lists the
    input of one benchmark test."""
    # We initialize a Pandas DataFrame with a list of all the trees, the
    # resulting DataFrame has the following columns:
    # - Tree type: This is the tree type, ADG or HDT
    # - TLS version: The TLS version corresponding to the tree.
    # - Tree: A reference to the tree itself.
    # - Model: A list of models names for this tree.
    tree_list = []
    for tree_type, tls_versions in trees.items():
        for version, tree in tls_versions.items():
            tree_list.append(
                {
                    "Tree type": tree_type,
                    "TLS version": version,
                    "Tree": tree,
                    "Model": list(tree.models),
                }
            )
    df = pandas.DataFrame(tree_list)

    # Instead of a list of models, we want a separate row for each model with
    # the rest of the values the same. This can be done with the `explode`
    # function.
    df = df.explode("Model")

    # We now have the basic dataset with all the trees and models that we want
    # to benchmark. The next step we perform, is adding input selectors. For
    # this we create a mapping between selectors and methods, which defaults to
    # using all input selectors. For ADG we only add "first", because there is
    # only one path and more input selection is irrelevant.
    input_selector_mapping = collections.defaultdict(
        lambda: list(INPUT_SELECTORS.keys())
    )
    input_selector_mapping["adg"] = ["first"]

    # We then create a dataframe from this mapping.
    input_selector_df = pandas.DataFrame(
        [
            {"Tree type": method, "Input selector": input_selector_mapping[method]}
            for method in df["Tree type"].unique()
        ]
    )
    # We merge this mapping and explode the Input selector column to get the
    # extended benchmarks.
    df = df.merge(input_selector_df, on="Tree type", how="left")
    df = df.explode("Input selector")

    # To distinguish between the HDT with different input selectors, we add the
    # "Method" field, which is the name of the Tree type in upper case with the
    # input selector. For ADG we only add the tree type
    not_adg_rows = df["Tree type"] != "adg"
    df["Method"] = "ADG"
    df["Method"][not_adg_rows] = (
        df["Tree type"][not_adg_rows].apply(str.upper)
        + " "
        + df["Input selector"][not_adg_rows].apply(str.capitalize)
    )

    # Some input selectors can use multiple weight functions. For now this only
    # applies to the Gini input selector, the rest is unaffected by weight and
    # is assigned the most simple "equal" function. We still evaluate for
    # different weights functions during the analysis, but running the
    # benchmark for different weight functions for "First" and "Random" is
    # merely a duplication.
    #
    # We apply the same trick as above to map the input selectors to the weight
    # functions. The default here is to only use the "equal" weight function.
    weight_function_mapping = collections.defaultdict(lambda: ["equal"])
    weight_function_mapping["gini"] = list(MODEL_WEIGHTS.keys())

    weight_function_df = pandas.DataFrame(
        [
            {
                "Input selector": selector,
                "Weight function": weight_function_mapping[selector],
            }
            for selector in df["Input selector"].unique()
        ]
    )

    # Merge the mapping and explode
    df = df.merge(weight_function_df, on="Input selector", how="left")
    df = df.explode("Weight function")

    # To distinguish between the different combinations of Gini and weight
    # functions, we add the weight function to the Method field.
    gini_rows = df["Input selector"] == "gini"
    df["Method"][gini_rows] = (
        df["Method"][gini_rows] + " (" + df["Weight function"][gini_rows] + ")"
    )

    # Lastly, to pass the iteration count to the benchmark function, add
    # a column to the dataframe.
    df["Iterations"] = iterations

    # Convert the dataframe to a list, as this is easier to distribute over
    # multiple processes.

    return df.to_dict(orient="records")


def benchmark_all(iterations=100):
    benchmark_inputs = generate_benchmark_inputs(iterations)

    # Apply the benchmark function to the inputs and show a progress bar.
    input_count = len(benchmark_inputs)
    with multiprocessing.Pool() as p:
        results = list(
            tqdm.tqdm(p.imap(benchmark, benchmark_inputs), total=input_count)
        )

    return results


def _weighted_stats(df, metric, weight):
    # Create the weighted stats object
    describer = weightstats.DescrStatsW(df[metric], df[weight])

    # Use this to create a series with the stats
    stats = pandas.Series()
    stats["mean"] = describer.mean
    stats["std"] = describer.std

    quantiles = describer.quantile([0, 0.25, 0.5, 0.75, 1])
    quantiles.index = ["min", "25%", "50%", "75%", "max"]

    stats = stats.append(quantiles)
    return stats


def visualize_weight_function(df, output_directory, weight_name):
    """Create visualizations for a specified weight function."""
    # Only the Gini method uses the different weight functions, the rest
    # doesn't use the weight in the decision process. We do want to compare the
    # performance when weighted, to see how a different usage of TLS
    # implementations would impact the different methods.

    # Start by creating a copy of the DataFrame, to prevent modifications to
    # the original
    df = df.copy()

    # For every model, compute the weight using the specified weight_function
    weight_function = MODEL_WEIGHTS[weight_name]

    def compute_weight(row):
        tree = row["Tree"]
        model = row["Model"]
        return weight_function(tree.model_mapping[model])

    df["Weight"] = df.apply(compute_weight, axis=1)

    # We then explode on the results, to treat all measurements as individual
    # records.
    df = df.explode("Results")

    # The results are still stored as dictionary. We extract them and put them
    # in a more descriptive column name.
    metric_mapping = [
        ("inputs", "Number of inputs"),
        ("resets", "Number of resets"),
        ("time", "Time in seconds"),
    ]

    for field, column in metric_mapping:
        df[column] = df["Results"].apply(operator.itemgetter(field))

    # Sort the data to set the column order
    df.sort_values(["TLS version", "Method"], inplace=True)

    # For every metric plot the results and create a markdown table
    for field, column in metric_mapping:
        title = f"{column} with weight function '{weight_name}'"
        output_path = output_directory / f"{field}_{weight_name}"

        plot_kwargs = {"stat": "probability", "common_norm": False}
        if field == "time":
            plot_kwargs["bins"] = 50
            rounding = 4
        else:
            plot_kwargs["binwidth"] = 1
            rounding = 2

        # Create a plot grid, with one graph for each combination of TLS
        # version and Method. Important: specify the weight column.
        seaborn.set_theme(font_scale=2, style="whitegrid")
        graph = seaborn.displot(
            x=column,
            col="TLS version",
            row="Method",
            weights="Weight",
            data=df,
            facet_kws={"margin_titles": True},
            **plot_kwargs,
        )
        graph.set_titles(col_template="{col_name}", row_template="{row_name}")
        pyplot.savefig(output_path.with_suffix(".pdf"))
        pyplot.close()

        markdown = ""
        # Create a markdown table for each TLS version
        grouped_by_tls = df.groupby("TLS version")
        for tls_version, tls_group in grouped_by_tls:
            # Group by method
            grouped_by_method = tls_group.groupby("Method")

            # Create a summary consisting of weighted statistics.
            summary = grouped_by_method.apply(
                _weighted_stats, metric=column, weight="Weight"
            )
            summary = summary.round(rounding)

            # Convert to Markdown
            markdown += summary.to_markdown()

            # Add caption to table
            markdown += "\n\n"
            markdown += f": Benchmark summary: {title} for {tls_version}"

            # Some newline to separate the different tables
            markdown += "\n\n\n"

        # After generating a table for each TLS version write output to file
        with open(output_path.with_suffix(".md"), "w") as f:
            f.write(markdown)


def visualize_tls_models(df, output_directory, tls_version):
    """Create visualizations to show the (average) metric values for each
    model, for the given TLS version."""
    # Start by creating a copy of the DataFrame, to prevent modifications to
    # the original
    df = df.copy()

    # Only keep the rows with the correct TLS version
    df = df[df["TLS version"] == tls_version]

    # Explode the results and extract the metrics to separate columns
    metric_mapping = [
        ("inputs", "Number of inputs"),
        ("resets", "Number of resets"),
        ("time", "Time in seconds"),
    ]

    df = df.explode("Results")

    for field, column in metric_mapping:
        df[column] = df["Results"].apply(operator.itemgetter(field))

    # Compute the mean of the metrics for every method for every model
    grouped = df.groupby(["Model", "Method"])
    metric_averages = grouped.mean()

    # For each metric, create a table with Model as rows and Method as columns
    for field, column in metric_mapping:
        results = metric_averages[column].unstack()

        # Sort the models by number
        results = results.sort_index(
            key=lambda index: [int(name.split("-")[-1]) for name in index]
        )

        # Convert to Markdown
        markdown = results.to_markdown()

        # Add caption to table
        markdown += "\n\n"
        markdown += f": {column} average for each model of {tls_version}"

        # Write to output file
        with open(output_directory / f"{tls_version} {field}.md", "w") as f:
            f.write(markdown)


def visualize_all(benchmark_data, output_directory):
    # Make sure the output directory exists
    output_directory = pathlib.Path(output_directory)
    output_directory.mkdir(exist_ok=True)

    # Convert data to Pandas dataframe
    df = pandas.DataFrame(benchmark_data)

    # Include a reference to the original tree
    df["Tree"] = df.apply(
        lambda row: trees[row["Tree type"]][row["TLS version"]], axis=1
    )

    # Format the TLS version for prettier visualizations
    df["TLS version"] = df["TLS version"].apply(util.format_tls_string)

    for weight_function in MODEL_WEIGHTS.keys():
        visualize_weight_function(df, output_directory, weight_function)

    # Visualize metrics per model
    for tls_version in df["TLS version"].unique():
        visualize_tls_models(df, output_directory, tls_version)
