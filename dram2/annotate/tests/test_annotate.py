"""



"""

"""
import os
os.system("pytest dram2/annotate/tests/test_annotate.py")

"""



import pytest

import os
from io import StringIO
from functools import partial
from filecmp import cmp
from click.testing import CliRunner
from shutil import copy

import pandas as pd
import numpy as np
from skbio.io import read as read_sequence

from dram2.utils.utils import make_mmseqs_db, parse_hmmsearch_domtblout, get_sig_row
from dram2.utils.tests.test_utils import logger
from dram2.annotate.annotate import (
    filter_fasta,
    run_prodigal,
    get_best_hits,
    get_reciprocal_best_hits,
    process_reciprocal_best_hits,
    get_gene_data,
    get_unannotated,
    assign_grades,
    generate_annotated_fasta,
    create_annotated_fasta,
    generate_renamed_fasta,
    rename_fasta,
    do_blast_style_search,
    count_motifs,
    strip_endings,
    process_custom_dbs,
    get_dups,
    vogdb_hmmscan_formater,
    dbcan_hmmscan_formater,
    kofam_hmmscan_formater,
)


@pytest.fixture()
def fasta_loc():
    return os.path.join("tests", "data", "NC_001422.fasta")

@pytest.fixture()
def runner():
    return CliRunner()

def test_kofam(runner):
  result = runner.invoke(hello, ['Peter'])
  assert result.exit_code == 0
  assert result.output == 'Hello Peter!\n'


def test_filter_fasta(fasta_loc, tmpdir):
    filtered_seq_5000 = filter_fasta(fasta_loc, min_len=5000)
    assert len(filtered_seq_5000) == 1
    assert len(filtered_seq_5000[0]) == 5386
    filtered_seq_6000 = filter_fasta(fasta_loc, min_len=6000)
    assert len(filtered_seq_6000) == 0
    filt_fasta = tmpdir.mkdir("test_filt").join("filtered_fasta.fasta")
    filter_fasta(fasta_loc, min_len=5000, output_loc=str(filt_fasta))
    assert os.path.isfile(filt_fasta)


@pytest.fixture()
def prodigal_dir(fasta_loc, tmpdir, logger):
    prodigal_output = tmpdir.mkdir("prodigal_output")
    gff, fna, faa = run_prodigal(fasta_loc, str(prodigal_output), logger)
    return gff, fna, faa


@pytest.fixture()
def prodigal_gff(prodigal_dir):
    return prodigal_dir[0]


@pytest.fixture()
def prodigal_fna(prodigal_dir):
    return prodigal_dir[1]


@pytest.fixture()
def prodigal_faa(prodigal_dir):
    return prodigal_dir[2]


def test_run_prodigal(prodigal_gff, prodigal_fna, prodigal_faa):
    assert os.path.isfile(prodigal_gff)
    assert os.path.isfile(prodigal_fna)
    assert os.path.isfile(prodigal_faa)


@pytest.fixture()
def mmseqs_db_dir(tmpdir):
    output_loc = tmpdir.mkdir("make_mmseqs_db_test")
    return output_loc


@pytest.fixture()
def mmseqs_db(prodigal_faa, mmseqs_db_dir, logger):
    output_file = str(mmseqs_db_dir.join("mmseqs_db.mmsdb"))
    make_mmseqs_db(prodigal_faa, output_file, logger, True, 1)
    return output_file


@pytest.fixture()
def phix_proteins():
    return os.path.join("tests", "data", "NC_001422.faa")


@pytest.fixture()
def target_mmseqs_db(mmseqs_db_dir, phix_proteins, logger):
    output_file = str(mmseqs_db_dir.join("target.mmsdb"))
    make_mmseqs_db(phix_proteins, output_file, logger, True, 1)
    return output_file



@pytest.fixture()
def processed_hits():
    forward = os.path.join("tests", "data", "query_target_hits.b6")
    reverse = os.path.join("tests", "data", "target_query_hits.b6")
    processed_hits = process_reciprocal_best_hits(forward, reverse)
    return processed_hits


def test_process_reciprocal_best_hits(processed_hits):
    assert processed_hits.shape == (7, 5)
    assert set(processed_hits.loc[processed_hits.target_RBH].index) == {
        "NC_001422.1_5",
        "NC_001422.1_4",
        "NC_001422.1_7",
        "NC_001422.1_6",
    }


def test_get_sig_row():
    names = ["target_start", "target_end", "target_length", "full_evalue"]
    assert not get_sig_row(pd.Series([1, 85, 100, 1], index=names))
    assert get_sig_row(pd.Series([1, 86, 100, 1e-16], index=names))
    assert not get_sig_row(pd.Series([1, 29, 100, 1e-20], index=names))


@pytest.fixture()
def phix_prodigal_genes():
    phix_seq = (
        ">NC_001422.1_1 # 51 # 221 # 1 # ID=1_1;partial=00;start_type=ATG;rbs_motif=GGA/GAG/AGG;rbs_spacer=5-"
        "10bp;gc_cont=0.404\n"
        "MSRKIILIKQELLLLVYELNRSGLLAENEKIRPILAQLEKLLLCDLSPSTNDSVKN*\n"
        ">NC_001422.1_2 # 390 # 848 # 1 # ID=1_2;partial=00;start_type=ATG;rbs_motif=GGA/GAG/AGG;rbs_spacer="
        "5-10bp;gc_cont=0.468\n"
        "MSQVTEQSVRFQTALASIKLIQASAVLDLTEDDFDFLTSNKVWIATDRSRARRCVEACVY\n"
        "GTLDFVGYPRFPAPVEFIAAVIAYYVHPVNIQTACLIMEGAEFTENIINGVERPVKAAEL\n"
        "FAFTLRVRAGNTDVLTDAEENVRQKLRAEGVM*\n"
        ">NC_001422.1_3 # 848 # 964 # 1 # ID=1_3;partial=00;start_type=ATG;rbs_motif=AGGAG;rbs_spacer=5-10bp;"
        "gc_cont=0.496\n"
        "MSKGKKRSGARPGRPQPLRGTKGKRKGARLWYVGGQQF*\n"
        ">NC_001422.1_4 # 1001 # 2284 # 1 # ID=1_4;partial=00;start_type=ATG;rbs_motif=GGAG/GAGG;rbs_spacer=5"
        "-10bp;gc_cont=0.449\n"
        "MSNIQTGAERMPHDLSHLGFLAGQIGRLITISTTPVIAGDSFEMDAVGALRLSPLRRGLA\n"
        "IDSTVDIFTFYVPHRHVYGEQWIKFMKDGVNATPLPTVNTTGYIDHAAFLGTINPDTNKI\n"
        "PKHLFQGYLNIYNNYFKAPWMPDRTEANPNELNQDDARYGFRCCHLKNIWTAPLPPETEL\n"
        "SRQMTTSTTSIDIMGLQAAYANLHTDQERDYFMQRYHDVISSFGGKTSYDADNRPLLVMR\n"
        "SNLWASGYDVDGTDQTSLGQFSGRVQQTYKHSVPRFFVPEHGTMFTLALVRFPPTATKEI\n"
        "QYLNAKGALTYTDIAGDPVLYGNLPPREISMKDVFRSGDSSKKFKIAEGQWYRYAPSYVS\n"
        "PAYHLLEGFPFIQEPPSGDLQERVLIRHHDYDQCFQSVQLLQWNSQVKFNVTVYRNLPTT\n"
        "RDSIMTS*\n"
        ">NC_001422.1_5 # 2395 # 2922 # 1 # ID=1_5;partial=00;start_type=ATG;rbs_motif=AGGAG;rbs_spacer=5-10"
        "bp;gc_cont=0.420\n"
        "MFQTFISRHNSNFFSDKLVLTSVTPASSAPVLQTPKATSSTLYFDSLTVNAGNGGFLHCI\n"
        "QMDTSVNAANQVVSVGADIAFDADPKFFACLVRFESSSVPTTLPTAYDVYPLNGRHDGGY\n"
        "YTVKDCVTIDVLPRTPGNNVYVGFMVWSNFTATKCRGLVSLNQVIKEIICLQPLK*\n"
        ">NC_001422.1_6 # 2931 # 3917 # 1 # ID=1_6;partial=00;start_type=ATG;rbs_motif=GGAG/GAGG;rbs_spacer="
        "5-10bp;gc_cont=0.451\n"
        "MFGAIAGGIASALAGGAMSKLFGGGQKAASGGIQGDVLATDNNTVGMGDAGIKSAIQGSN\n"
        "VPNPDEAAPSFVSGAMAKAGKGLLEGTLQAGTSAVSDKLLDLVGLGGKSAADKGKDTRDY\n"
        "LAAAFPELNAWERAGADASSAGMVDAGFENQKELTKMQLDNQKEIAEMQNETQKEIAGIQ\n"
        "SATSRQNTKDQVYAQNEMLAYQQKESTARVASIMENTNLSKQQQVSEIMRQMLTQAQTAG\n"
        "QYFTNDQIKEMTRKVSAEVDLVHQQTQNQRYGSSHIGATAKDISNVVTDAASGVVDIFHG\n"
        "IDKAVADTWNNFWKDGKADGIGSNLSRK*\n"
        ">NC_001422.1_7 # 3981 # 5384 # 1 # ID=1_7;partial=01;start_type=ATG;rbs_motif=GGAGG;rbs_spacer=5-10"
        "bp;gc_cont=0.455\n"
        "MVRSYYPSECHADYFDFERIEALKPAIEACGISTLSQSPMLGFHKQMDNRIKLLEEILSF\n"
        "RMQGVEFDNGDMYVDGHKAASDVRDEFVSVTEKLMDELAQCYNVLPQLDINNTIDHRPEG\n"
        "DEKWFLENEKTVTQFCRKLAAERPLKDIRDEYNYPKKKGIKDECSRLLEASTMKSRRGFA\n"
        "IQRLMNAMRQAHADGWFIVFDTLTLADDRLEAFYDNPNALRDYFRDIGRMVLAAEGRKAN\n"
        "DSHADCYQYFCVPEYGTANGRLHFHAVHFMRTLPTGSVDPNFGRRVRNRRQLNSLQNTWP\n"
        "YGYSMPIAVRYTQDAFSRSGWLWPVDAKGEPLKATSYMAVGFYVAKYVNKKSDMDLAAKG\n"
        "LGAKEWNNSLKTKLSLLPKKLFRIRMSRNFGMKMLTMTNLSTECLIQLTKLGYDATPFNQ\n"
        "ILKQNAKREMRLRLGKVTVADVLAAQPVTTNLLKFMRASIKMIGVSNL\n"
    )
    return StringIO(phix_seq)


# Do this better
def test_get_gene_data(phix_prodigal_genes):
    scaffold_gene_df = get_gene_data(phix_prodigal_genes)
    assert scaffold_gene_df.shape == (7, 5)


def test_get_unannotated(phix_proteins):
    annotated_genes = [
        "NP_040704.1",
        "NP_040703.1",
        "NP_040713.1",
        "NP_040712.1",
        "NP_040711.1",
        "NP_040710.1",
        "NP_040709.1",
        "NP_040707.1",
    ]
    unannotated_genes = ["NP_040705.1", "NP_040706.1", "NP_040708.1"]
    test_unannotated_genes = get_unannotated(phix_proteins, annotated_genes)
    assert set(unannotated_genes) == set(test_unannotated_genes)


def test_assign_grades():
    annotations_data = [
        [True, "K00001", False, "KOER09234OK", ["PF00001"]],
        [False, "K00002", True, "KLODKJFSO234KL", ["PF01234"]],
        [False, "K00003", False, "EIORWU234KLKDS", np.NaN],
        [False, np.NaN, False, np.NaN, np.NaN],
        [False, np.NaN, False, np.NaN, ["PF01235"]],
    ]
    annotations = pd.DataFrame(
        annotations_data,
        index=["gene1", "gene2", "gene3", "gene4", "gene5"],
        columns=["kegg_RBH", "kegg_hit", "uniref_RBH", "uniref_hit", "pfam_hits"],
    )
    test_grades = assign_grades(annotations)
    assert test_grades.loc["gene1", "rank"] == "A"
    assert test_grades.loc["gene2", "rank"] == "B"
    assert test_grades.loc["gene3", "rank"] == "C"
    assert test_grades.loc["gene4", "rank"] == "E"
    assert test_grades.loc["gene5", "rank"] == "D"
    # test no uniref
    annotations_data2 = [
        [True, "K00001", ["PF00001"]],
        [False, "K00002", ["PF01234"]],
        [False, "K00003", np.NaN],
        [False, np.NaN, np.NaN],
        [False, np.NaN, ["PF01235"]],
    ]
    annotations2 = pd.DataFrame(
        annotations_data2,
        index=["gene1", "gene2", "gene3", "gene4", "gene5"],
        columns=["kegg_RBH", "kegg_hit", "pfam_hits"],
    )
    test_grades2 = assign_grades(annotations2)
    assert test_grades2.loc["gene1", "rank"] == "A"
    assert test_grades2.loc["gene2", "rank"] == "C"
    assert test_grades2.loc["gene3", "rank"] == "C"
    assert test_grades2.loc["gene4", "rank"] == "E"
    assert test_grades2.loc["gene5", "rank"] == "D"


@pytest.fixture()
def phix_annotations():
    return pd.DataFrame(
        [
            ["A", "K1", "U1", None, "a_bug1"],
            ["B", "K2", "U2", "P2", "a_bug2"],
            ["C", None, "U3", "P3", "a_bug3"],
            ["D", "K4", "U4", "P4", "a_bug4"],
            ["A", "K5", "U5", "P5", "a_bug5"],
            ["E", "K6", "U6", "P6", "a_bug6"],
            ["C", "K7", "U7", "P7", "a_bug7"],
        ],
        index=[
            "NC_001422.1_1",
            "NC_001422.1_2",
            "NC_001422.1_3",
            "NC_001422.1_4",
            "NC_001422.1_5",
            "NC_001422.1_6",
            "NC_001422.1_7",
        ],
        columns=["rank", "kegg_hit", "uniref_hit", "pfam_hits", "bin_taxonomy"],
    )


def test_generate_annotated_fasta_short(phix_prodigal_genes, phix_annotations):
    fasta_generator_short = generate_annotated_fasta(
        phix_prodigal_genes, phix_annotations, verbosity="short", name="phiX"
    )
    short_fasta_header_dict = {
        seq.metadata["id"]: seq.metadata["description"] for seq in fasta_generator_short
    }

    assert short_fasta_header_dict["phiX_NC_001422.1_1"] == "rank: A; K1 (db=kegg)"
    assert short_fasta_header_dict["phiX_NC_001422.1_2"] == "rank: B; U2 (db=uniref)"
    assert short_fasta_header_dict["phiX_NC_001422.1_4"] == "rank: D; P4 (db=pfam)"


@pytest.fixture()
def phix_annotations_no_kegg():
    return pd.DataFrame(
        [
            ["B", "U1", None, "a_bug1"],
            ["B", "U2", "P2", "a_bug2"],
            ["C", "U3", "P3", "a_bug3"],
            ["D", "U4", "P4", "a_bug4"],
            ["B", "U5", "P5", "a_bug5"],
            ["E", "U6", "P6", "a_bug6"],
            ["C", "U7", "P7", "a_bug7"],
        ],
        index=[
            "NC_001422.1_1",
            "NC_001422.1_2",
            "NC_001422.1_3",
            "NC_001422.1_4",
            "NC_001422.1_5",
            "NC_001422.1_6",
            "NC_001422.1_7",
        ],
        columns=["rank", "uniref_hit", "pfam_hits", "bin_taxonomy"],
    )


def test_generate_annotated_fasta_short_no_kegg(
    phix_prodigal_genes, phix_annotations_no_kegg
):
    # drop kegg hit column
    fasta_generator_short_no_kegg = generate_annotated_fasta(
        phix_prodigal_genes, phix_annotations_no_kegg, verbosity="short", name="phiX"
    )
    short_fasta_header_dict_no_kegg = {
        seq.metadata["id"]: seq.metadata["description"]
        for seq in fasta_generator_short_no_kegg
    }
    print(short_fasta_header_dict_no_kegg)

    assert (
        short_fasta_header_dict_no_kegg["phiX_NC_001422.1_1"]
        == "rank: B; U1 (db=uniref)"
    )
    assert (
        short_fasta_header_dict_no_kegg["phiX_NC_001422.1_2"]
        == "rank: B; U2 (db=uniref)"
    )
    assert (
        short_fasta_header_dict_no_kegg["phiX_NC_001422.1_4"] == "rank: D; P4 (db=pfam)"
    )


def test_generate_annotated_fasta_long(phix_prodigal_genes, phix_annotations):
    fasta_generator_long = generate_annotated_fasta(
        phix_prodigal_genes, phix_annotations, verbosity="long", name="phiX"
    )
    long_fasta_header_dict = {
        seq.metadata["id"]: seq.metadata["description"] for seq in fasta_generator_long
    }
    assert (
        long_fasta_header_dict["phiX_NC_001422.1_1"]
        == "rank: A; K1 (db=kegg); U1 (db=uniref); a_bug1"
    )


def test_create_annotated_fasta(phix_prodigal_genes, phix_annotations, tmpdir):
    filt_fasta = tmpdir.mkdir("test_annotate_fasta").join("annotated_fasta.faa")
    create_annotated_fasta(phix_prodigal_genes, phix_annotations, str(filt_fasta))
    assert os.path.isfile(filt_fasta)


def test_generate_renamed_fasta(fasta_loc):
    genome_fasta_header_dict = {
        seq.metadata["id"]: seq.metadata["description"]
        for seq in generate_renamed_fasta(fasta_loc, "phiX")
    }
    assert len(genome_fasta_header_dict) == 1
    assert (
        genome_fasta_header_dict["phiX_NC_001422.1"]
        == "Coliphage phi-X174, complete genome"
    )


def test_rename_fasta(fasta_loc, tmpdir):
    filt_fasta = tmpdir.mkdir("test_annotate_scaffolds").join("annotated_fasta.fasta")
    rename_fasta(fasta_loc, str(filt_fasta), "phiX")
    assert os.path.isfile(filt_fasta)


class FakeDatabaseHandler:
    @staticmethod
    def get_database_names():
        return []


def test_count_motifs(phix_proteins):
    motif_counts = count_motifs(phix_proteins, motif="(A.A)")
    assert len(motif_counts) == 11
    assert motif_counts["NP_040713.1"] == 7
    assert motif_counts["NP_040709.1"] == 0


def test_strip_endings():
    assert strip_endings("abc.efg", [".efg", ".jkl"]) == "abc"
    assert strip_endings("abc.jkl", [".efg", ".jkl"]) == "abc"
    assert strip_endings("123456", [".efg", ".jkl"]) == "123456"


def test_process_custom_dbs(phix_proteins, tmpdir, logger):
    custom_db_dir = tmpdir.mkdir("custom_dbs")
    process_custom_dbs(
        [phix_proteins], ["phix"], os.path.join(custom_db_dir, "custom_dbs0"), logger
    )
    assert os.path.isfile(
        os.path.join(custom_db_dir, "custom_dbs0", "phix.custom.mmsdb")
    )
    process_custom_dbs(None, None, os.path.join(custom_db_dir, "custom_dbs1"), logger)
    assert len(os.listdir(os.path.join(custom_db_dir, "custom_dbs1"))) == 0
    with pytest.raises(ValueError):
        process_custom_dbs(
            ["thing1", "thing2"],
            ["thing1"],
            os.path.join(custom_db_dir, "custom_dbs2"),
            logger,
        )


def test_get_dubs():
    w_dups = [True, False, True, True]
    assert get_dups(w_dups) == [True, True, False, False]
    no_dups = ["a", "b", "c"]
    assert get_dups(no_dups) == [True, True, True]


def test_parse_hmmsearch_domtblout():
    parsed_hit = parse_hmmsearch_domtblout(
        os.path.join("tests", "data", "hmmsearch_hit.txt")
    )
    assert parsed_hit.shape == (1, 23)
    assert parsed_hit.loc[0, "query_id"] == "NP_040710.1"
    assert parsed_hit.loc[0, "query_length"] == 38
    assert parsed_hit.loc[0, "target_id"] == "Microvir_J"
    assert parsed_hit.loc[0, "target_ascession"] == "PF04726.13"
    assert parsed_hit.loc[0, "full_evalue"] == 6.900000e-31


@pytest.fixture()
def annotated_fake_gff_loc():
    return os.path.join("tests", "data", "annotated_fake_gff.gff")


@pytest.fixture()
def fake_phix_annotations():
    return pd.DataFrame(
        [[np.NaN], ["GH13"], [np.NaN], [np.NaN], [np.NaN], [np.NaN], [np.NaN]],
        index=[
            "NC_001422.1_1",
            "NC_001422.1_2",
            "NC_001422.1_3",
            "NC_001422.1_4",
            "NC_001422.1_5",
            "NC_001422.1_6",
            "NC_001422.1_7",
        ],
        columns=["cazy_id"],
    )


@pytest.fixture()
def fake_gff_loc():
    return os.path.join("tests", "data", "fake_gff.gff")


def test_kofam_hmmscan_formater_dbcan():
    output_expt = pd.DataFrame(
        {
            "bin_1.scaffold_1": ["K00001", "KO1; description 1"],
            "bin_1.scaffold_2": ["K00002", "KO2; description 2"],
        },
        index=["ko_id", "kegg_hit"],
    ).T
    input_b6 = os.path.join("tests", "data", "unformatted_kofam.b6")
    hits = parse_hmmsearch_domtblout(input_b6)
    output_rcvd = kofam_hmmscan_formater(
        hits=hits,
        hmm_info_path=os.path.join("tests", "data", "hmm_thresholds.txt"),
        top_hit=True,
        use_dbcan2_thresholds=True,
    )
    output_rcvd.sort_index(inplace=True)
    output_expt.sort_index(inplace=True)
    assert output_rcvd.equals(
        output_expt
    ), "Error in kofam_hmmscan_formater with dbcam cuts"


def test_kofam_hmmscan_formater():
    output_expt = pd.DataFrame(
        {"bin_1.scaffold_2": ["K00002", "KO2; description 2"]},
        index=["ko_id", "kegg_hit"],
    ).T
    input_b6 = os.path.join("tests", "data", "unformatted_kofam.b6")
    hits = parse_hmmsearch_domtblout(input_b6)
    output_rcvd = kofam_hmmscan_formater(
        hits=hits,
        hmm_info_path=os.path.join("tests", "data", "hmm_thresholds.txt"),
        top_hit=True,
        use_dbcan2_thresholds=False,
    )
    output_rcvd.sort_index(inplace=True)
    output_expt.sort_index(inplace=True)
    assert output_rcvd.equals(output_expt), "Error in kofam_hmmscan_formater"


def test_vogdb_hmmscan_formater():
    # TODO Not Done
    output_expt = pd.DataFrame(
        {"bin_1.scaffold_1": "VOG00001", "bin_1.scaffold_2": "VOG00002"},
        index=["vogdb_id"],
    ).T
    input_b6 = os.path.join("tests", "data", "unformatted_vogdb.b6")
    hits = parse_hmmsearch_domtblout(input_b6)
    output_rcvd = vogdb_hmmscan_formater(hits=hits, db_name="vogdb")
    output_rcvd.sort_index(inplace=True)
    output_expt.sort_index(inplace=True)
    assert output_rcvd.equals(output_expt), "Error in vogdb_hmmscan_formater"
